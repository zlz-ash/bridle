"""Unified project-session Agent Gateway."""
from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.container.candidate_contract import FrozenTestContract
from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.container.container_service import get_shared_container_backend
from bridle.agent.container.test_backend import ModuleContainerTestBackend
from bridle.agent.container.test_command_compiler import TestCommandCompiler
from bridle.agent.memory.short_term_memory import ShortTermMemory
from bridle.agent.providers.agent_provider import AgentProviderFactory
from bridle.agent.runtime.agent_runtime import RuntimeHandle, RuntimeRole, RuntimeState
from bridle.agent.runtime.authorization import (
    AgentAuthorizationService,
    AgentIdentity,
    AgentRole,
    BudgetGrant,
)
from bridle.agent.runtime.change_outbox import ChangeOutbox, ChangeOutboxForwarder
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationState,
    ModificationWorkflow,
)
from bridle.agent.runtime.parent_child_runtime import ParentChildRuntimeCoordinator
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_registry import (
    ProjectRuntimeStopAllResult,
    ProjectRuntimeStopFailure,
    get_project_runtime_registry,
)
from bridle.agent.runtime.role_policy import RuntimeRolePolicy
from bridle.agent.runtime.schemas import AgentContext
from bridle.agent.runtime.session_runtime_lifecycle import RuntimeSessionLifecycle
from bridle.agent.runtime.verification_orchestrator import (
    CandidateVerificationExecutor,
    VerificationOrchestrator,
)
from bridle.agent.skills.registry import SkillRegistry
from bridle.api.errors import ConflictError
from bridle.features.project_map.patch_schemas import PlanPatchSchema
from bridle.features.project_map.plan_service import PlanService
from bridle.features.project_map.service import ProjectMapService
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.sessions.schemas import ProjectMessageReadSchema
from bridle.features.sessions.service import ProjectSessionService
from bridle.logging.facade import get_logging_facade
from bridle.models.agent_runtime import AgentRuntimeRecord, RuntimeInputDeliveryRecord
from bridle.observability.context import current_log_context

Provider = Callable[[str], Awaitable[str]]


@dataclass
class _SessionMemoryState:
    memory: ShortTermMemory
    lock: asyncio.Lock
    needs_recovery: bool = True


class SessionMemoryWindowManager:
    """Keep one incremental memory window per active project session."""

    def __init__(
        self,
        *,
        budget: int = 4000,
        recent_window: int = 4,
        optimizer: Callable[[str, list[dict]], Awaitable[str]] | None = None,
    ) -> None:
        self._budget = budget
        self._recent_window = recent_window
        self._optimizer = optimizer
        self._states: dict[str, _SessionMemoryState] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}

    def turn_lock(self, session_id: str) -> asyncio.Lock:
        """Return the single admission boundary for one session's complete turn."""
        return self._turn_locks.setdefault(session_id, asyncio.Lock())

    async def context_for_turn(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        current_message: ProjectMessageReadSchema,
    ) -> list[dict]:
        state = self._states.get(session_id)
        if state is None:
            state = _SessionMemoryState(
                memory=ShortTermMemory(
                    budget=self._budget,
                    recent_window=self._recent_window,
                    optimizer=self._optimizer,
                    run_id=session_id,
                ),
                lock=asyncio.Lock(),
            )
            self._states[session_id] = state

        async with state.lock:
            cold_start = state.needs_recovery
            try:
                if cold_start:
                    checkpoint = await ProjectSessionService.get_memory_checkpoint(db, session_id)
                    anchor = None if checkpoint is None else checkpoint.anchor_message_id
                    delta = await ProjectSessionService.list_messages_after(
                        db,
                        session_id,
                        after_message_id=anchor,
                    )
                    messages = [message.model_dump(mode="json") for message in delta]
                    current_payload = current_message.model_dump(mode="json")
                    if not any(message.get("id") == current_message.id for message in messages):
                        messages.append(current_payload)
                    state.memory.restore(
                        summary="" if checkpoint is None else checkpoint.summary,
                        messages=[],
                        anchor_message_id=anchor,
                    )
                else:
                    messages = [current_message.model_dump(mode="json")]

                previous_anchor = state.memory.anchor_message_id
                window = await state.memory.append(messages)
                new_anchor = state.memory.anchor_message_id
                if new_anchor is not None and new_anchor != previous_anchor:
                    await ProjectSessionService.update_memory_checkpoint(
                        db,
                        session_id,
                        summary=state.memory.summary,
                        anchor_message_id=new_anchor,
                    )
                state.needs_recovery = False
                get_logging_facade().info_event(
                    "session_memory_window.update",
                    "completed",
                    session_id=session_id,
                    detail={
                        "cold_start": cold_start,
                        "appended_count": len(messages),
                        "window_count": len(window),
                        "checkpoint_advanced": new_anchor != previous_anchor,
                    },
                )
                return [
                    message
                    for message in window
                    if message.get("id") != current_message.id
                ]
            except asyncio.CancelledError:
                state.needs_recovery = True
                get_logging_facade().info_event(
                    "session_memory_window.update",
                    "cancelled",
                    session_id=session_id,
                    detail={
                        "cold_start": cold_start,
                        "current_message_id": current_message.id,
                    },
                )
                raise

    async def record_persisted_message(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        message: ProjectMessageReadSchema,
    ) -> None:
        """Advance the hot window after an assistant message is durably persisted."""
        state = self._states.get(session_id)
        if state is None:
            get_logging_facade().info_event(
                "session_memory_window.persisted_message",
                "skipped",
                session_id=session_id,
                detail={"message_id": message.id, "role": message.role},
            )
            return

        async with state.lock:
            try:
                previous_anchor = state.memory.anchor_message_id
                window = await state.memory.append([message.model_dump(mode="json")])
                new_anchor = state.memory.anchor_message_id
                if new_anchor is not None and new_anchor != previous_anchor:
                    await ProjectSessionService.update_memory_checkpoint(
                        db,
                        session_id,
                        summary=state.memory.summary,
                        anchor_message_id=new_anchor,
                    )
                get_logging_facade().info_event(
                    "session_memory_window.persisted_message",
                    "completed",
                    session_id=session_id,
                    detail={
                        "message_id": message.id,
                        "role": message.role,
                        "window_count": len(window),
                        "checkpoint_advanced": new_anchor != previous_anchor,
                    },
                )
            except asyncio.CancelledError:
                state.needs_recovery = True
                get_logging_facade().info_event(
                    "session_memory_window.persisted_message",
                    "cancelled",
                    session_id=session_id,
                    detail={"message_id": message.id, "role": message.role},
                )
                raise

    def drop(self, session_id: str) -> None:
        self._states.pop(session_id, None)
        turn_lock = self._turn_locks.get(session_id)
        if turn_lock is None or not turn_lock.locked():
            self._turn_locks.pop(session_id, None)


_session_memory_manager: SessionMemoryWindowManager | None = None


def _memory_manager() -> SessionMemoryWindowManager:
    global _session_memory_manager
    if _session_memory_manager is None:
        _session_memory_manager = SessionMemoryWindowManager(
            optimizer=AgentProviderFactory.create_memory_optimizer(),
        )
    return _session_memory_manager


@dataclass
class _ParentJob:
    sequence_no: int
    message_id: str
    provider: Provider
    trace_id: str
    future: asyncio.Future[ProjectMessageReadSchema]


class _ParentRuntimeWorker:
    """Let the Host-owned parent task claim Mail in durable sequence order."""

    def __init__(
        self,
        coordinator: ParentChildRuntimeCoordinator,
        mailbox: PersistentMailbox,
        address: AgentAddress,
        session_id: str,
        store,
        recovery_provider: Provider,
    ) -> None:
        self.coordinator = coordinator
        self.mailbox = mailbox
        self.address = address
        self.session_id = session_id
        self.store = store
        self.recovery_provider = recovery_provider
        self._jobs: dict[str, _ParentJob] = {}
        self._jobs_changed = asyncio.Condition()
        self._settled_message_ids: set[str] = set()
        self._recovery_pending = True
        self._claim_targets = tuple(
            AgentAddress(address.project_id, address.agent_id, generation)
            for generation in range(1, address.generation + 1)
        )

    async def submit(
        self,
        *,
        sequence_no: int,
        message_id: str,
        provider: Provider,
        trace_id: str,
    ) -> ProjectMessageReadSchema:
        future = asyncio.get_running_loop().create_future()
        job = _ParentJob(sequence_no, message_id, provider, trace_id, future)
        async with self._jobs_changed:
            self._jobs[message_id] = job
            self._jobs_changed.notify_all()
        return await future

    async def _wait_for_any_job(self) -> None:
        async with self._jobs_changed:
            await self._jobs_changed.wait_for(lambda: bool(self._jobs))

    async def _job_for(self, message_id: str) -> _ParentJob:
        async with self._jobs_changed:
            await self._jobs_changed.wait_for(lambda: message_id in self._jobs)
            return self._jobs.pop(message_id)

    async def _cancel_pending_jobs(self) -> None:
        async with self._jobs_changed:
            jobs = tuple(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            if not job.future.done():
                job.future.cancel()

    async def _restore_job(self, job: _ParentJob) -> None:
        async with self._jobs_changed:
            self._jobs.setdefault(job.message_id, job)
            self._jobs_changed.notify_all()

    async def _ack_with_retry(
        self,
        *,
        message_id: str,
        lease_token: str,
        target: AgentAddress,
        trace_id: str,
    ) -> str:
        for attempt in range(1, 4):
            acknowledged = self.mailbox.ack(
                message_id,
                lease_token,
                target=target,
            )
            if acknowledged.status == "acked":
                return "acked"
            if acknowledged.status == "lost_lease":
                get_logging_facade().warn_event(
                    "runtime_mail.lease_lost",
                    "deferred",
                    trace_id=trace_id,
                    message_id=message_id,
                    project_id=self.address.project_id,
                    agent_id=self.address.agent_id,
                    generation=self.address.generation,
                    session_id=self.session_id,
                    error_code="mail_lost_lease",
                    detail={"attempt": attempt},
                )
                return "lost_lease"
            get_logging_facade().warn_event(
                "runtime_mail.ack_retry",
                "retry",
                trace_id=trace_id,
                message_id=message_id,
                project_id=self.address.project_id,
                agent_id=self.address.agent_id,
                generation=self.address.generation,
                session_id=self.session_id,
                error_code=f"mail_{acknowledged.status}",
                detail={"attempt": attempt},
            )
            if attempt < 3:
                await asyncio.sleep(0.01)
        return "deferred"

    async def run(self, _handle: RuntimeHandle) -> None:
        claimed = None
        claim_target = self.address
        active_job: _ParentJob | None = None
        try:
            while True:
                claimed = None
                if self._recovery_pending:
                    for recovery_target in self._claim_targets:
                        candidate = self.mailbox.claim(recovery_target)
                        if candidate.status == "claimed":
                            claimed = candidate
                            claim_target = recovery_target
                            break
                    if claimed is None:
                        self._recovery_pending = False
                        continue
                else:
                    await self._wait_for_any_job()
                    for recovery_target in self._claim_targets:
                        candidate = self.mailbox.claim(recovery_target)
                        if candidate.status == "claimed":
                            claimed = candidate
                            claim_target = recovery_target
                            break
                    if claimed is None:
                        await asyncio.sleep(0.01)
                        continue
                if claimed.status != "claimed":
                    await asyncio.sleep(0.01)
                    continue
                if claimed.lease_token is None or claimed.message_id is None:
                    raise RuntimeError("runtime_mail_claim_invalid")
                if claimed.message_id in self._settled_message_ids:
                    ack_status = await self._ack_with_retry(
                        message_id=claimed.message_id,
                        lease_token=claimed.lease_token,
                        target=claim_target,
                        trace_id=f"runtime-settled-{claimed.message_id}",
                    )
                    if ack_status == "acked":
                        self._settled_message_ids.discard(claimed.message_id)
                        get_logging_facade().info_event(
                            "runtime_mail.settled",
                            "completed",
                            message_id=claimed.message_id,
                            project_id=self.address.project_id,
                            agent_id=self.address.agent_id,
                            generation=self.address.generation,
                            session_id=self.session_id,
                            detail={"attempt": claimed.attempt},
                        )
                    elif ack_status == "deferred":
                        self.mailbox.nack(
                            claimed.message_id,
                            claimed.lease_token,
                            target=claim_target,
                        )
                    claimed = None
                    continue
                envelope = claimed.envelope
                if envelope is not None and envelope.message_type == "child-result":
                    payload = envelope.payload
                    try:
                        self.store.apply_child_result(
                            message_id=claimed.message_id,
                            node_id=str(payload["node_id"]),
                            status=str(payload["status"]),
                            result=dict(payload.get("result") or {}),
                        )
                    except Exception as exc:
                        self.mailbox.nack(
                            claimed.message_id,
                            claimed.lease_token,
                            target=claim_target,
                        )
                        get_logging_facade().warn_event(
                            "runtime_child.result_recovery_retry",
                            "retry",
                            message_id=claimed.message_id,
                            project_id=self.address.project_id,
                            agent_id=self.address.agent_id,
                            generation=self.address.generation,
                            session_id=self.session_id,
                            error_code=type(exc).__name__,
                            detail={"attempt": claimed.attempt},
                        )
                        claimed = None
                        continue
                    ack_status = await self._ack_with_retry(
                        message_id=claimed.message_id,
                        lease_token=claimed.lease_token,
                        target=claim_target,
                        trace_id=f"runtime-recovery-{claimed.message_id}",
                    )
                    if ack_status == "deferred":
                        self.mailbox.nack(
                            claimed.message_id,
                            claimed.lease_token,
                            target=claim_target,
                        )
                        claimed = None
                        continue
                    if ack_status == "lost_lease":
                        self._settled_message_ids.add(claimed.message_id)
                    get_logging_facade().info_event(
                        "runtime_child.result_recovered",
                        "completed",
                        message_id=claimed.message_id,
                        project_id=self.address.project_id,
                        agent_id=self.address.agent_id,
                        generation=self.address.generation,
                        session_id=self.session_id,
                        detail={"attempt": claimed.attempt, "ack_status": ack_status},
                    )
                    claimed = None
                    continue
                if claim_target.generation < self.address.generation:
                    recovery_trace_id = f"runtime-recovery-{claimed.message_id}"
                    try:
                        reply = await self.coordinator.handle_input(
                            claimed.message_id,
                            self.recovery_provider,
                            trace_id=recovery_trace_id,
                        )
                    except Exception as exc:
                        ack_status = await self._ack_with_retry(
                            message_id=claimed.message_id,
                            lease_token=claimed.lease_token,
                            target=claim_target,
                            trace_id=recovery_trace_id,
                        )
                        if ack_status == "deferred":
                            self.mailbox.nack(
                                claimed.message_id,
                                claimed.lease_token,
                                target=claim_target,
                            )
                        elif ack_status == "lost_lease":
                            self._settled_message_ids.add(claimed.message_id)
                        get_logging_facade().warn_event(
                            "runtime_input.recovery_failed",
                            "failed",
                            trace_id=recovery_trace_id,
                            message_id=claimed.message_id,
                            project_id=self.address.project_id,
                            agent_id=self.address.agent_id,
                            generation=self.address.generation,
                            session_id=self.session_id,
                            error_code=type(exc).__name__,
                            detail={
                                "attempt": claimed.attempt,
                                "source_generation": claim_target.generation,
                                "ack_status": ack_status,
                            },
                        )
                        claimed = None
                        continue
                    ack_status = await self._ack_with_retry(
                        message_id=claimed.message_id,
                        lease_token=claimed.lease_token,
                        target=claim_target,
                        trace_id=recovery_trace_id,
                    )
                    if ack_status == "deferred":
                        self.mailbox.nack(
                            claimed.message_id,
                            claimed.lease_token,
                            target=claim_target,
                        )
                        claimed = None
                        continue
                    if ack_status == "lost_lease":
                        self._settled_message_ids.add(claimed.message_id)
                    get_logging_facade().info_event(
                        "runtime_input.recovered",
                        "completed",
                        trace_id=recovery_trace_id,
                        message_id=claimed.message_id,
                        project_id=self.address.project_id,
                        agent_id=self.address.agent_id,
                        generation=self.address.generation,
                        session_id=self.session_id,
                        detail={
                            "attempt": claimed.attempt,
                            "source_generation": claim_target.generation,
                            "reply_message_id": reply.id,
                            "ack_status": ack_status,
                        },
                    )
                    claimed = None
                    continue
                active_job = await self._job_for(claimed.message_id)
                if active_job.sequence_no != claimed.sequence_no:
                    raise RuntimeError("runtime_mail_sequence_mismatch")
                try:
                    reply = await self.coordinator.handle_input(
                        active_job.message_id,
                        active_job.provider,
                        trace_id=active_job.trace_id,
                    )
                except Exception as exc:
                    ack_status = await self._ack_with_retry(
                        message_id=active_job.message_id,
                        lease_token=claimed.lease_token,
                        target=claim_target,
                        trace_id=active_job.trace_id,
                    )
                    if ack_status == "deferred":
                        self.mailbox.nack(
                            active_job.message_id,
                            claimed.lease_token,
                            target=claim_target,
                        )
                        await self._restore_job(active_job)
                    else:
                        if ack_status == "lost_lease":
                            self._settled_message_ids.add(active_job.message_id)
                    if ack_status != "deferred" and not active_job.future.done():
                        get_logging_facade().warn_event(
                            "runtime_input.failed",
                            "failed",
                            trace_id=active_job.trace_id,
                            message_id=active_job.message_id,
                            project_id=self.address.project_id,
                            agent_id=self.address.agent_id,
                            generation=self.address.generation,
                            session_id=self.session_id,
                            error_code=type(exc).__name__,
                            detail={"attempt": claimed.attempt, "ack_status": ack_status},
                        )
                        active_job.future.set_exception(exc)
                else:
                    ack_status = await self._ack_with_retry(
                        message_id=active_job.message_id,
                        lease_token=claimed.lease_token,
                        target=claim_target,
                        trace_id=active_job.trace_id,
                    )
                    if ack_status == "deferred":
                        self.mailbox.nack(
                            active_job.message_id,
                            claimed.lease_token,
                            target=claim_target,
                        )
                        await self._restore_job(active_job)
                    else:
                        if ack_status == "lost_lease":
                            self._settled_message_ids.add(active_job.message_id)
                    if ack_status != "deferred" and not active_job.future.done():
                        get_logging_facade().info_event(
                            "runtime_input.delivered",
                            "completed",
                            trace_id=active_job.trace_id,
                            message_id=active_job.message_id,
                            project_id=self.address.project_id,
                            agent_id=self.address.agent_id,
                            generation=self.address.generation,
                            session_id=self.session_id,
                            detail={"attempt": claimed.attempt, "ack_status": ack_status},
                        )
                        active_job.future.set_result(reply)
                active_job = None
                claimed = None
        except asyncio.CancelledError:
            if (
                claimed is not None
                and claimed.message_id is not None
                and claimed.lease_token is not None
            ):
                self.mailbox.nack(
                    claimed.message_id,
                    claimed.lease_token,
                    target=claim_target,
                )
            if active_job is not None and not active_job.future.done():
                active_job.future.cancel()
            await self._cancel_pending_jobs()
            raise

_runtime_components_lock = asyncio.Lock()
_runtime_components: dict[
    int,
    tuple[object, AgentRuntimeHost, ParentChildRuntimeCoordinator],
] = {}
_parent_workers: dict[str, _ParentRuntimeWorker] = {}
_parent_runtime_lock = asyncio.Lock()


@dataclass
class _OutboxForwarderHandle:
    stop: asyncio.Event
    task: asyncio.Task[None]
    mailbox: PersistentMailbox


_outbox_forwarders: dict[tuple[str, str], _OutboxForwarderHandle] = {}
_outbox_forwarder_lock = asyncio.Lock()


@dataclass
class _VerificationLoopHandle:
    stop: asyncio.Event
    wake: asyncio.Event
    task: asyncio.Task[None]


_verification_loops: dict[tuple[str, str], _VerificationLoopHandle] = {}
_verification_loop_lock = asyncio.Lock()


def _verification_executor_for(store: ProjectPlanStore) -> CandidateVerificationExecutor:
    return CandidateVerificationExecutor(store)


async def _ensure_verification_loop(
    *,
    store: ProjectPlanStore,
    project_id: str,
) -> _VerificationLoopHandle:
    key = (str(store.project_root), project_id)
    async with _verification_loop_lock:
        current = _verification_loops.get(key)
        if current is not None and not current.task.done():
            return current
        stop = asyncio.Event()
        wake = asyncio.Event()
        orchestrator = VerificationOrchestrator(
            store,
            _verification_executor_for(store),
        )

        async def run() -> None:
            while not stop.is_set():
                try:
                    await orchestrator.recover()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    get_logging_facade().error_event(
                        "verification_loop_failed",
                        "retry",
                        project_id=project_id,
                        error_code=type(exc).__name__,
                        detail={"reason": "verification_recovery_failed"},
                    )
                next_retry_at = orchestrator.next_retry_at()
                delay = 1.0
                if next_retry_at is not None:
                    delay = min(delay, max(0.0, next_retry_at - time.time()))
                if delay <= 0:
                    await asyncio.sleep(0)
                    continue
                stop_wait = asyncio.create_task(stop.wait())
                wake_wait = asyncio.create_task(wake.wait())
                done, pending = await asyncio.wait(
                    {stop_wait, wake_wait},
                    timeout=delay,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if wake_wait in done:
                    wake.clear()

        task = asyncio.create_task(
            run(),
            name=f"verification-loop:{project_id}",
        )
        handle = _VerificationLoopHandle(stop=stop, wake=wake, task=task)
        _verification_loops[key] = handle
        return handle


async def _ensure_change_outbox_forwarder(
    *,
    project_path: str,
    project_id: str,
    facade,
    trace_id: str,
) -> _OutboxForwarderHandle:
    key = (str(Path(project_path).resolve()), project_id)
    async with _outbox_forwarder_lock:
        current = _outbox_forwarders.get(key)
        if current is not None and not current.task.done():
            return current
        if current is not None:
            await current.mailbox.close()
        outbox = ChangeOutbox(
            project_path,
            project_id=project_id,
            facade=facade,
        )
        outbox.recover()
        mailbox = PersistentMailbox(
            Path(project_path) / ".bridle" / "mail.db",
            project_id=project_id,
            consumer_id="change-outbox-forwarder",
            facade=facade,
            trace_id=trace_id,
        )
        stop = asyncio.Event()
        async def wake_map_runtime(_intent) -> None:
            await get_project_runtime_registry().wake(project_id, project_path)

        forwarder = ChangeOutboxForwarder(
            outbox,
            mailbox,
            wake_callback=wake_map_runtime,
        )
        task = asyncio.create_task(
            forwarder.run(stop),
            name=f"change-outbox-forwarder:{project_id}",
        )
        handle = _OutboxForwarderHandle(stop=stop, task=task, mailbox=mailbox)
        _outbox_forwarders[key] = handle
        return handle


async def recover_project_runtime(
    *,
    project_path: str,
    project_id: str,
    facade,
) -> None:
    """Recover one project's durable Outbox/Mail before request admission."""
    root = Path(project_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError("project_path_unavailable")
    store = ProjectPlanStore(root, project_id=project_id, facade=facade)
    if store.database_path.is_file():
        store.overview()
    else:
        store.initialize(scan_if_created=False)
    verification = VerificationOrchestrator(store, _verification_executor_for(store))
    await verification.recover()
    await _ensure_verification_loop(store=store, project_id=project_id)
    outbox = ChangeOutbox(root, project_id=project_id, facade=facade)
    outbox.recover()
    mailbox = PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id="startup-outbox-recovery",
        facade=facade,
    )
    try:
        outbox.publish_ready(mailbox)
    finally:
        await mailbox.close()
    registry = get_project_runtime_registry()
    agent = await registry.wake_if_pending(project_id, root)
    if agent is not None and agent.task is not None:
        await agent.task
    await _ensure_change_outbox_forwarder(
        project_path=str(root),
        project_id=project_id,
        facade=facade,
        trace_id=f"recovery-{project_id}",
    )


async def _components_for(
    db: AsyncSession,
) -> tuple[AgentRuntimeHost, ParentChildRuntimeCoordinator]:
    bind = db.bind
    if bind is None:
        raise RuntimeError("runtime_database_bind_required")
    key = id(bind)
    async with _runtime_components_lock:
        current = _runtime_components.get(key)
        if current is not None and current[0] is bind:
            return current[1], current[2]
        sessions = async_sessionmaker(bind, expire_on_commit=False)
        host = AgentRuntimeHost(sessions)
        coordinator = ParentChildRuntimeCoordinator(sessions)
        _runtime_components[key] = (bind, host, coordinator)
        return host, coordinator


async def _parent_generation(
    db: AsyncSession,
    host: AgentRuntimeHost,
    *,
    project_id: str,
    session_id: str,
) -> int:
    for handle in host.active_handles():
        if (
            handle.spec.role is RuntimeRole.PARENT
            and handle.spec.project_id == project_id
            and handle.spec.session_id == session_id
            and handle.state is not RuntimeState.DESTROYED
        ):
            return handle.spec.generation
    latest = await db.scalar(
        select(func.max(AgentRuntimeRecord.generation)).where(
            AgentRuntimeRecord.runtime_type == RuntimeRole.PARENT.value,
            AgentRuntimeRecord.project_id == project_id,
            AgentRuntimeRecord.session_id == session_id,
        )
    )
    return int(latest or 0) + 1


async def _ensure_parent_runtime(
    host: AgentRuntimeHost,
    coordinator: ParentChildRuntimeCoordinator,
    *,
    project_path: str,
    project_id: str,
    session_id: str,
    generation: int,
    tools: dict,
    store,
    recovery_provider: Provider,
) -> tuple[RuntimeHandle, _ParentRuntimeWorker]:
    for handle in host.active_handles():
        if (
            handle.spec.role is RuntimeRole.PARENT
            and handle.spec.project_id == project_id
            and handle.spec.session_id == session_id
            and handle.state is not RuntimeState.DESTROYED
        ):
            return handle, _parent_workers[handle.spec.runtime_id]
    authorization = AgentAuthorizationService()
    grant = authorization.resolve(
        identity=AgentIdentity(
            principal_id=f"session:{session_id}",
            role=AgentRole.COORDINATOR,
            project_id=project_id,
            session_id=session_id,
            agent_id=f"session-{session_id}",
        ),
        policy_version="session-runtime-v1",
    )
    address = AgentAddress(project_id, f"session-{session_id}", generation)
    mailbox = PersistentMailbox(
        Path(project_path) / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id=f"parent-runtime-{session_id}",
        default_target=address,
    )
    worker = _ParentRuntimeWorker(
        coordinator,
        mailbox,
        address,
        session_id,
        store,
        recovery_provider,
    )
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id=project_id,
        agent_id=f"session-{session_id}",
        generation=generation,
        grant=grant,
        session_id=session_id,
        tools=tools,
        task_factory=worker.run,
        mailbox=mailbox,
    )
    existing = _parent_workers.get(handle.spec.runtime_id)
    if existing is not None:
        await mailbox.close()
        return handle, existing
    _parent_workers[handle.spec.runtime_id] = worker
    return handle, worker


async def shutdown_gateway_runtimes():
    """Destroy HTTP-owned runtimes before the application releases its database."""
    registry = get_project_runtime_registry()
    await registry.begin_shutdown()
    failures: list[ProjectRuntimeStopFailure] = []
    async with _verification_loop_lock:
        verification_loops = list(_verification_loops.items())
        _verification_loops.clear()
    for _, handle in verification_loops:
        handle.stop.set()
        handle.wake.set()
    if verification_loops:
        loop_results = await asyncio.gather(
            *(handle.task for _, handle in verification_loops),
            return_exceptions=True,
        )
        for ((_, project_id), _), result in zip(
            verification_loops,
            loop_results,
            strict=True,
        ):
            if isinstance(result, BaseException):
                failures.append(
                    ProjectRuntimeStopFailure(
                        project_id,
                        "verification_loop_stop_failed",
                        type(result).__name__,
                    )
                )
    async with _outbox_forwarder_lock:
        forwarders = list(_outbox_forwarders.items())
        _outbox_forwarders.clear()
    for _, handle in forwarders:
        handle.stop.set()
    if forwarders:
        task_results = await asyncio.gather(
            *(handle.task for _, handle in forwarders),
            return_exceptions=True,
        )
        close_results = await asyncio.gather(
            *(handle.mailbox.close() for _, handle in forwarders),
            return_exceptions=True,
        )
        for ((_, project_id), _), result in zip(forwarders, task_results, strict=True):
            if isinstance(result, BaseException):
                failures.append(
                    ProjectRuntimeStopFailure(
                        project_id,
                        "change_outbox_forwarder_stop_failed",
                        type(result).__name__,
                    )
                )
        for ((_, project_id), _), result in zip(forwarders, close_results, strict=True):
            if isinstance(result, BaseException):
                failures.append(
                    ProjectRuntimeStopFailure(
                        project_id,
                        "change_outbox_forwarder_mailbox_close_failed",
                        type(result).__name__,
                    )
                )
    registry_result = await registry.stop_all()
    failures.extend(registry_result.failures)
    async with _runtime_components_lock:
        components = list(_runtime_components.values())
        _runtime_components.clear()
        _parent_workers.clear()
    for _, host, _ in components:
        parents = [
            handle
            for handle in host.active_handles()
            if handle.spec.role is RuntimeRole.PARENT
        ]
        for handle in parents:
            try:
                await host.destroy(handle)
            except Exception as exc:
                failures.append(
                    ProjectRuntimeStopFailure(
                        handle.spec.project_id,
                        "gateway_parent_runtime_destroy_failed",
                        type(exc).__name__,
                    )
                )
        for handle in host.active_handles():
            try:
                await host.destroy(handle)
            except Exception as exc:
                failures.append(
                    ProjectRuntimeStopFailure(
                        handle.spec.project_id,
                        "gateway_runtime_destroy_failed",
                        type(exc).__name__,
                    )
                )
    return ProjectRuntimeStopAllResult(tuple(failures))


async def _execute_child_work(store, *, node_id: str, target_role: str) -> dict:
    """Execute the smallest real dispatched-child unit; tests may inject terminal outcomes."""
    node = store.get_node(node_id)
    if node["status"] != target_role:
        raise RuntimeError("child_dispatch_state_mismatch")
    await asyncio.sleep(0)
    result = (
        store.subgraph(node_id, depth=1)
        if target_role == "mapping"
        else store.module_execution_snapshot(node_id)
    )
    if result.get("error_code"):
        raise ConflictError(
            resource="module_boundary",
            message="Child execution snapshot is incomplete",
            error_code=str(result["error_code"]),
            details=dict(result.get("detail") or {}),
        )
    return {"node_id": node_id, "target_role": target_role, "result": result}


class AgentGateway:
    """Run planning and execution turns through one project-scoped runtime."""

    @staticmethod
    async def close_session(db: AsyncSession, session_id: str):
        """Destroy this session's Gateway runtimes and close it without deleting history."""
        session = await ProjectSessionService.get(db, session_id)
        host, _ = await _components_for(db)
        runtime_ids = {
            handle.spec.runtime_id
            for handle in host.active_handles()
            if handle.spec.session_id == session_id
        }
        bind = db.bind
        if bind is None:
            raise RuntimeError("runtime_database_bind_required")
        lifecycle = RuntimeSessionLifecycle(
            async_sessionmaker(bind, expire_on_commit=False),
            host=host,
            trace_id=current_log_context().get("trace_id"),
        )
        await lifecycle.close_session(session_id)
        for runtime_id in runtime_ids:
            _parent_workers.pop(runtime_id, None)
        if _session_memory_manager is not None:
            _session_memory_manager.drop(session_id)
        db.expire_all()
        return await ProjectSessionService.get(db, session.id)

    @staticmethod
    async def converse(
        db: AsyncSession,
        session_id: str,
        content: str,
        *,
        node_id: str | None = None,
    ) -> ProjectMessageReadSchema:
        """Serialize one session's complete turn before any durable input is created."""
        manager = _memory_manager()
        turn_lock = manager.turn_lock(session_id)
        facade = get_logging_facade()
        waiting = turn_lock.locked()
        started = time.monotonic()
        acquired = False
        facade.info_event(
            "session_turn.admission",
            "started",
            session_id=session_id,
            detail={"waiting": waiting},
        )
        if waiting:
            facade.info_event(
                "session_turn.admission_waiting",
                "started",
                session_id=session_id,
            )
        try:
            await turn_lock.acquire()
            acquired = True
            waited_ms = int((time.monotonic() - started) * 1000)
            if waiting:
                facade.info_event(
                    "session_turn.admission_waiting",
                    "completed",
                    session_id=session_id,
                    detail={"waited_ms": waited_ms},
                )
            facade.info_event(
                "session_turn.admission",
                "completed",
                session_id=session_id,
                detail={"waited_ms": waited_ms},
            )
            return await AgentGateway._converse_serialized(
                db,
                session_id,
                content,
                node_id=node_id,
            )
        except asyncio.CancelledError:
            facade.info_event(
                "session_turn.admission",
                "cancelled",
                session_id=session_id,
                detail={"acquired": acquired},
            )
            raise
        finally:
            if acquired:
                turn_lock.release()
                facade.info_event(
                    "session_turn.admission_released",
                    "completed",
                    session_id=session_id,
                )

    @staticmethod
    async def _converse_serialized(
        db: AsyncSession,
        session_id: str,
        content: str,
        *,
        node_id: str | None = None,
    ) -> ProjectMessageReadSchema:
        """Run one shared turn; session/content/node input exits as a persisted assistant message."""
        session = await ProjectSessionService.get(db, session_id)
        if session.status != "active":
            raise ConflictError(
                resource="project_session",
                message="Closed sessions retain history but cannot accept new turns",
                error_code="project_session_closed",
            )
        if not session.available or not Path(session.project_path).is_dir():
            raise ConflictError(
                resource="project_session",
                message="Project path is unavailable; history is read-only",
                error_code="project_unavailable_read_only",
            )
        facade = get_logging_facade()
        request_trace = current_log_context().get("trace_id")
        runtime_trace_id = (
            str(request_trace)
            if request_trace
            else f"runtime-turn-{secrets.token_hex(8)}"
        )
        started = time.monotonic()
        facade.info_event(
            "project_agent_turn",
            "started",
            trace_id=runtime_trace_id,
            session_id=session_id,
            detail={"project_id": session.project_id, "role": session.role},
        )
        execution_node: dict | None = None
        candidate_setup = None
        container_test_backend = None
        try:
            store = await ProjectMapService.store_for(db, session.project_id)
            readiness = store.readiness()
            if not readiness["can_chat"]:
                raise ConflictError(
                    resource="project_map",
                    message="Project map is not ready for chat",
                    error_code="project_map_not_ready",
                    details=readiness,
                )
            if session.role == "executing":
                if node_id is None:
                    raise ConflictError(
                        resource="project_session",
                        message="Executing turns require an explicit plan node",
                        error_code="execution_node_required",
                    )
                execution_node = store.get_node(node_id)
                if execution_node["status"] != "running":
                    execution_node = store.start_node(node_id)
            elif node_id is not None:
                raise ConflictError(
                    resource="project_session",
                    message="Planning turns cannot select an execution node",
                    error_code="planning_node_forbidden",
                )

            runtime_host, runtime_coordinator = await _components_for(db)
            parent_generation = await _parent_generation(
                db,
                runtime_host,
                project_id=session.project_id,
                session_id=session_id,
            )
            parent_handle: RuntimeHandle | None = None
            child_result_start = asyncio.Event()
            child_handles: list[RuntimeHandle] = []
            parent_address = AgentAddress(
                session.project_id,
                f"session-{session_id}",
                parent_generation,
            )
            input_message = await ProjectSessionService.create_runtime_input(
                db,
                session_id,
                content=content,
                target=parent_address,
                facade=facade,
                trace_id=runtime_trace_id,
            )
            mailbox = PersistentMailbox(
                Path(session.project_path) / ".bridle" / "mail.db",
                project_id=session.project_id,
                consumer_id=f"gateway-{session_id}",
                default_target=parent_address,
                facade=facade,
                trace_id=runtime_trace_id,
            )
            try:
                mail_result = mailbox.enqueue(
                    MailEnvelope(
                        message_id=input_message.id,
                        message_type="runtime-input",
                        source=AgentAddress(session.project_id, "session-gateway", 1),
                        target=parent_address,
                        payload={
                            "session_id": session_id,
                            "session_message_id": input_message.id,
                        },
                    )
                )
            finally:
                await mailbox.close()
            if mail_result.status not in {"inserted", "existing"}:
                raise ConflictError(
                    resource="project_session",
                    message="Runtime input is durably pending for retry",
                    error_code="runtime_input_pending",
                    details={"message_id": input_message.id},
                )
            delivery = (
                await db.execute(
                    select(RuntimeInputDeliveryRecord).where(
                        RuntimeInputDeliveryRecord.message_id == input_message.id
                    )
                )
            ).scalar_one()
            delivery.status = "delivered"
            delivery.attempt += 1
            delivery.mail_enqueued_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
            memory = await _memory_manager().context_for_turn(
                db,
                session_id,
                current_message=input_message,
            )
            overview = store.overview()
            skill_ids = SkillRegistry.default().list_ids()
            capabilities = RuntimeRolePolicy.manifest(session.role)
            allowed_files = [] if execution_node is None else list(execution_node.get("files") or [])
            node_tests = [] if execution_node is None else list(execution_node.get("tests") or [])
            readonly_files: list[str] = []
            workspace_root = session.project_path
            candidate_id: str | None = None
            if execution_node is not None:
                readonly_files = store.mock_readonly_paths_for_node(execution_node["id"])
                allowed_files = sorted(set(allowed_files) | set(readonly_files))
                snapshot = store.module_execution_snapshot(execution_node["id"])
                if snapshot.get("error_code"):
                    raise ConflictError(
                        resource="module_boundary",
                        message="Module execution snapshot is incomplete",
                        error_code=str(snapshot["error_code"]),
                        details=snapshot.get("detail") or {},
                    )
                active_contract_row = store.get_active_test_contract(
                    execution_node["id"]
                )
                test_contract = (
                    None
                    if active_contract_row is None
                    else FrozenTestContract.from_dict(active_contract_row["snapshot"])
                )
                modification = store.get_modification_workflow(execution_node["id"])
                formal_contract_active = bool(
                    test_contract is not None
                    and modification is not None
                    and modification["state"]
                    in {
                        ModificationState.RED_VERIFYING.value,
                        ModificationState.IMPLEMENTING.value,
                    }
                )
                candidate_base_map_seq = (
                    test_contract.map_seq
                    if formal_contract_active and test_contract is not None
                    else store.latest_change_seq()
                )
                candidate_service = CandidateExecutionService(session.project_path)
                candidate_setup = candidate_service.prepare(
                    run_id=session_id,
                    node=execution_node,
                    base_map_seq=candidate_base_map_seq,
                    readonly_files=readonly_files,
                    map_snapshot=snapshot,
                )
                candidate_id = candidate_setup.candidate_id
                workspace_root = str(candidate_setup.workspace.project_dir)
                allowed_files = sorted(set(allowed_files) | set(candidate_setup.workspace.write_set))
                node_tests = list(snapshot.get("test_commands") or node_tests)
                facade.info_event(
                    "candidate_created",
                    "completed",
                    session_id=session_id,
                    detail={
                        "project_id": session.project_id,
                        "node_id": execution_node["id"],
                        "candidate_id": candidate_id,
                        "module_id": candidate_setup.module_id,
                        "base_map_seq": candidate_base_map_seq,
                    },
                )
                red_verification = bool(
                    test_contract is not None
                    and modification is not None
                    and modification["state"] == ModificationState.RED_VERIFYING.value
                )
                if red_verification and test_contract is not None:
                    node_tests = [command.raw_command for command in test_contract.commands]
                    required_command_ids = [
                        command.command_id for command in test_contract.commands
                    ]
                    test_map_seq = test_contract.map_seq
                else:
                    approved_commands = TestCommandCompiler.compile_commands(
                        test_commands=node_tests,
                        test_entity_id=execution_node["id"],
                        map_seq=candidate_base_map_seq,
                    )
                    required_command_ids = [
                        command.command_id for command in approved_commands
                    ]
                    test_map_seq = candidate_base_map_seq
                container_backend = get_shared_container_backend(session.project_path)
                container_test_backend = ModuleContainerTestBackend(
                    container_backend,
                    candidate_request=candidate_setup.request,
                    candidate_root=str(candidate_setup.workspace.root),
                    module_root=str(candidate_setup.workspace.module_root),
                    candidate_rel=candidate_setup.workspace.candidate_rel,
                    test_entity_id=execution_node["id"],
                    required_commands=node_tests,
                    required_command_ids=required_command_ids,
                    map_seq=test_map_seq,
                    test_contract=test_contract,
                    red_verification=red_verification,
                )
            context_node = execution_node or {
                "id": "project-runtime",
                "title": session.title,
                "goal": "Continue the project plan and execute only when permitted.",
                "node_type": "project_session",
                "depends_on": [],
            }
            if candidate_setup is None:
                await _ensure_change_outbox_forwarder(
                    project_path=session.project_path,
                    project_id=session.project_id,
                    facade=facade,
                    trace_id=runtime_trace_id,
                )
            capabilities["sandbox"] = {
                "run_id": session_id,
                "node_id": context_node["id"],
                "workspace_root": workspace_root,
                "project_root": session.project_path,
                "project_id": session.project_id,
                "agent_id": f"session-{session_id}",
                "generation": parent_generation,
                "trace_id": runtime_trace_id,
                "formal_workspace": candidate_setup is None,
                "allowed_files": allowed_files,
                "readonly_files": readonly_files,
                "node_tests": node_tests,
                "network_allowed": False,
                "candidate_id": candidate_id,
            }
            context = AgentContext(
                instruction=content,
                node=context_node,
                allowed_files=allowed_files,
                tests=node_tests,
                short_term_memory=memory,
                accessible_context={
                    "project_map": overview,
                    "skill_ids": skill_ids,
                    "session_role": session.role,
                },
                tool_capabilities=capabilities,
            )

            async def read_project_map(arguments: dict) -> dict:
                """Read one bounded map view; tool arguments exit through the existing store queries."""
                RuntimeRolePolicy.require(session.role, "read_project_map")
                mode = str(arguments.get("mode", "overview"))
                limit = max(1, min(int(arguments.get("limit", 50)), 200))
                if mode == "overview":
                    return store.overview()
                if mode == "node":
                    return store.get_node(str(arguments.get("node_id", "")))
                if mode == "children":
                    return store.children(
                        parent_id=arguments.get("parent_id"),
                        cursor=arguments.get("cursor"),
                        limit=limit,
                    )
                if mode == "subgraph":
                    depth = max(0, min(int(arguments.get("depth", 1)), 5))
                    return store.subgraph(str(arguments.get("node_id", "")), depth=depth, limit=limit)
                if mode == "search":
                    return store.search(
                        str(arguments.get("query", "")),
                        cursor=arguments.get("cursor"),
                        limit=limit,
                    )
                if mode == "execution":
                    wait_id = str(arguments.get("wait_id", "")).strip()
                    if not wait_id:
                        raise ValueError("wait_id is required")
                    return store.read_execution(wait_id)
                raise ValueError("Unsupported project map read mode")

            async def propose_semantic_annotation(arguments: dict) -> dict:
                RuntimeRolePolicy.require(session.role, "propose_semantic_annotation")
                mapping_seed = None
                if session.role == "mapping":
                    seed_id = arguments.get("seed_id")
                    if not seed_id:
                        raise ConflictError(
                            resource="map_blind_spot",
                            message="Mapping queries require an open blind spot seed",
                            error_code="blind_spot_seed_required",
                        )
                    mapping_seed = str(seed_id)
                return store.propose_semantic_annotation(
                    source_id=str(arguments.get("source_id", "")),
                    summary=str(arguments.get("summary", "")),
                    evidence=dict(arguments.get("evidence") or {}),
                    model=str(arguments.get("model", "agent")),
                    confidence=float(arguments.get("confidence", 0.0)),
                    file_hash=str(arguments.get("file_hash", "")),
                    risk=str(arguments.get("risk", "low")),
                    mapping_seed=mapping_seed,
                )

            async def dispatch_child_agent(arguments: dict) -> dict:
                RuntimeRolePolicy.require(session.role, "dispatch_child_agent")
                spawn = store.dispatch_child_agent(
                    str(arguments.get("node_id", "")),
                    target_role=str(arguments.get("target_role", "mapping")),
                )
                if parent_handle is None:
                    raise RuntimeError("runtime_parent_required")
                target_role = str(arguments.get("target_role", "mapping"))
                child_agent_id = f"child-{spawn['spawn_message_id']}"
                child_grant = AgentAuthorizationService().derive(
                    parent_handle.grant,
                    identity=AgentIdentity(
                        principal_id=child_agent_id,
                        role=(
                            AgentRole.PROJECT_MAPPER
                            if target_role == "mapping"
                            else AgentRole.IMPLEMENTER
                        ),
                        project_id=session.project_id,
                        session_id=session_id,
                        agent_id=child_agent_id,
                    ),
                    resource_scopes=(),
                    tool_grants=(),
                    skill_grants=(),
                    budget_grant=BudgetGrant(),
                )

                async def run_child(child_handle: RuntimeHandle) -> None:
                    status = "completed"
                    await runtime_host.transition(
                        child_handle,
                        RuntimeState.RUNNING,
                        reason="child_work_started",
                    )
                    try:
                        await child_result_start.wait()
                        result = await _execute_child_work(
                            store,
                            node_id=str(spawn["node_id"]),
                            target_role=target_role,
                        )
                    except asyncio.CancelledError:
                        status = "cancelled"
                        result = {
                            "error_code": "cancelled",
                            "message": "Child runtime was cancelled",
                        }
                    except Exception as exc:
                        status = "failed"
                        api_error = getattr(exc, "api_error", None)
                        result = {
                            "error_code": str(
                                getattr(api_error, "code", type(exc).__name__)
                            ),
                            "message": str(exc),
                        }
                        error_detail = getattr(api_error, "details", None)
                        if error_detail:
                            result["detail"] = dict(error_detail)
                    await runtime_host.transition(
                        child_handle,
                        {
                            "completed": RuntimeState.COMPLETED,
                            "failed": RuntimeState.FAILED,
                            "cancelled": RuntimeState.CANCELLED,
                        }[status],
                        reason=f"child_work_{status}",
                    )
                    result_message_id = f"child-result-{spawn['spawn_message_id']}"
                    result_mailbox = PersistentMailbox(
                        Path(session.project_path) / ".bridle" / "mail.db",
                        project_id=session.project_id,
                        consumer_id=f"child-result-{session_id}",
                        default_target=parent_address,
                        facade=facade,
                        trace_id=runtime_trace_id,
                    )
                    try:
                        delivered = False
                        for attempt in range(1, 4):
                            delivered = await runtime_coordinator.deliver_child_result(
                                message_id=result_message_id,
                                source=AgentAddress(
                                    session.project_id,
                                    child_agent_id,
                                    child_handle.spec.generation,
                                ),
                                target=parent_address,
                                payload={
                                    "node_id": str(spawn["node_id"]),
                                    "status": status,
                                    "target_role": target_role,
                                    "result": result,
                                },
                                apply_result=lambda result_id, payload: store.apply_child_result(
                                    message_id=result_id,
                                    node_id=str(payload["node_id"]),
                                    status=str(payload["status"]),
                                    result=dict(payload.get("result") or {}),
                                ),
                                destroy=lambda: asyncio.sleep(0),
                                mailbox=result_mailbox,
                                trace_id=runtime_trace_id,
                            )
                            if delivered:
                                break
                            if attempt < 3:
                                await asyncio.sleep(0.01)
                        if not delivered:
                            store.apply_child_result(
                                message_id=result_message_id,
                                node_id=str(spawn["node_id"]),
                                status=status,
                                result=result,
                            )
                            facade.warn_event(
                                "runtime_child.result_persisted_without_mail",
                                "completed",
                                trace_id=runtime_trace_id,
                                message_id=result_message_id,
                                project_id=session.project_id,
                                agent_id=child_agent_id,
                                generation=child_handle.spec.generation,
                                error_code="mail_backpressure",
                                detail={"attempt": 3, "status": status},
                            )
                    finally:
                        await result_mailbox.close()

                child = await runtime_host.create_runtime(
                    role=RuntimeRole.CHILD,
                    project_id=session.project_id,
                    agent_id=child_agent_id,
                    generation=1,
                    grant=child_grant,
                    session_id=session_id,
                    parent=parent_handle,
                    task_factory=run_child,
                )
                child_handles.append(child)
                return {**spawn, "runtime_id": child.spec.runtime_id}

            async def patch_plan_nodes(arguments: dict) -> dict:
                """Apply a local plan patch; tool arguments exit only through PlanService.patch_current."""
                RuntimeRolePolicy.require(session.role, "patch_plan_nodes")
                patch = PlanPatchSchema.model_validate(arguments)
                return await PlanService.patch_current(db, session.project_id, patch)

            async def execute_plan_node(arguments: dict) -> dict:
                """Create/reuse the fixed node's durable wait and return without awaiting work."""
                RuntimeRolePolicy.require(session.role, "execute_plan_node")
                requested_id = str(arguments.get("node_id", "")).strip()
                if not requested_id:
                    raise ValueError("node_id is required")
                if execution_node is None or requested_id != execution_node["id"]:
                    raise ConflictError(
                        resource="plan_node",
                        message="Cannot switch execution nodes during an active turn",
                        error_code="execution_node_switch_forbidden",
                    )
                return store.create_node_execution(
                    node_id=requested_id,
                    owner_address=parent_address.to_uri(),
                )

            runtime_tool_handlers = {
                "read_project_map": read_project_map,
                "propose_semantic_annotation": propose_semantic_annotation,
                "dispatch_child_agent": dispatch_child_agent,
                "patch_plan_nodes": patch_plan_nodes,
                "execute_plan_node": execute_plan_node,
            }
            parent_ready = asyncio.Event()
            provider_names: list[str] = []

            async def generate_reply(provider_content: str) -> str:
                await parent_ready.wait()
                provider_context = context.model_copy(
                    update={"instruction": provider_content}
                )
                provider = AgentProviderFactory.create(
                    provider_context,
                    runtime_tool_handlers=runtime_tool_handlers,
                    test_backend=container_test_backend,
                )
                provider_names.append(provider.name)
                proposal = await provider.generate(provider_context)
                facade.info_event(
                    "agent_terminal_decision",
                    "completed",
                    trace_id=runtime_trace_id,
                    session_id=session_id,
                    detail={
                        "terminal_status": proposal.terminal_status,
                        "reason": proposal.reason,
                        "provider": provider.name,
                    },
                )
                if proposal.terminal_status == "blocked":
                    return f"[blocked] {proposal.reason}"
                return proposal.summary

            async with _parent_runtime_lock:
                parent_handle, parent_worker = await _ensure_parent_runtime(
                    runtime_host,
                    runtime_coordinator,
                    project_path=session.project_path,
                    project_id=session.project_id,
                    session_id=session_id,
                    generation=parent_generation,
                    tools=runtime_tool_handlers,
                    store=store,
                    recovery_provider=generate_reply,
                )
            parent_ready.set()

            parent_completed = False
            try:
                assistant = await parent_worker.submit(
                    sequence_no=int(mail_result.sequence_no or 0),
                    message_id=input_message.id,
                    provider=generate_reply,
                    trace_id=runtime_trace_id,
                )
                parent_completed = True
                await _memory_manager().record_persisted_message(
                    db,
                    session_id,
                    message=assistant,
                )
            finally:
                if not parent_completed:
                    for child_handle in child_handles:
                        if child_handle.task is not None and not child_handle.task.done():
                            child_handle.task.cancel()
                child_result_start.set()
                for child_handle in child_handles:
                    try:
                        if child_handle.task is not None:
                            await child_handle.task
                    finally:
                        await runtime_host.destroy(child_handle)
            if candidate_setup is not None and execution_node is not None:
                workflow = ModificationWorkflow(store)
                current = workflow.current(execution_node["id"])
                if (
                    current is not None
                    and current["state"] == ModificationState.IMPLEMENTING.value
                ):
                    submitted = workflow.apply(
                        execution_node["id"],
                        event=ModificationEvent.SUBMITTED,
                        event_id=f"candidate:{candidate_setup.candidate_id}:submitted",
                        expected_revision=int(current["revision"]),
                        payload={"candidate_id": candidate_setup.candidate_id},
                    )
                    verification_loop = await _ensure_verification_loop(
                        store=store,
                        project_id=session.project_id,
                    )
                    verification_loop.wake.set()
                    facade.info_event(
                        "candidate_submitted",
                        "completed",
                        session_id=session_id,
                        detail={
                            "project_id": session.project_id,
                            "node_id": execution_node["id"],
                            "candidate_id": candidate_setup.candidate_id,
                            "revision": submitted["revision"],
                        },
                    )
                else:
                    facade.info_event(
                        "candidate_submission_skipped",
                        "skipped",
                        session_id=session_id,
                        error_code="candidate_workflow_not_implementing",
                        detail={
                            "project_id": session.project_id,
                            "node_id": execution_node["id"],
                            "candidate_id": candidate_setup.candidate_id,
                            "workflow_state": (
                                None if current is None else current["state"]
                            ),
                        },
                    )
            facade.info_event(
                "project_agent_turn",
                "completed",
                trace_id=runtime_trace_id,
                session_id=session_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail={
                    "project_id": session.project_id,
                    "role": session.role,
                    "memory_count": len(memory),
                    "root_count": len(overview["roots"]),
                    "skill_count": len(skill_ids),
                    "provider": provider_names[-1],
                },
            )
            return assistant
        except Exception as exc:
            facade.info_event(
                "project_agent_turn",
                "failed",
                trace_id=runtime_trace_id,
                session_id=session_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_code=type(exc).__name__,
                detail={"project_id": session.project_id, "role": session.role},
            )
            raise

