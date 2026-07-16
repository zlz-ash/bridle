from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Any

from bridle.agent.runtime.agent_runtime import RuntimeHandle, RuntimeState
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.mailbox import AgentAddress, MailboxResult
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.features.project_map.store import ProjectPlanStore
from bridle.logging.facade import LoggingFacade, get_logging_facade


class ProjectMapAgentState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOP_FAILED = "stop_failed"
    STOPPED = "stopped"
    FAILED = "failed"


class ProjectRuntimeShutdownError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "project_runtime_shutdown_failed",
        failures: tuple[Any, ...] = (),
        cancelled: bool = False,
    ) -> None:
        self.error_code = error_code
        self.failures = failures
        self.cancelled = cancelled
        super().__init__(message)


RetireCallback = Callable[[str, int, int], Awaitable[bool]]
CommitHook = Callable[[], None]


class ProjectMapAgent:
    """Short-lived consumer that applies durable CodeChanged mail to plan.db."""

    def __init__(
        self,
        project_id: str,
        project_root: str | Path,
        *,
        generation: int,
        mailbox: PersistentMailbox,
        retire_callback: RetireCallback,
        logging_facade: LoggingFacade | None = None,
        batch_size: int = 64,
        receive_timeout: float = 0.05,
        after_commit_hook: CommitHook | None = None,
    ) -> None:
        self.project_id = project_id
        self.canonical_path = Path(project_root).expanduser().resolve()
        self.generation = generation
        self.mailbox = mailbox
        self.target = AgentAddress(project_id, "map-runtime", 1)
        self._retire_callback = retire_callback
        self._logging = logging_facade or get_logging_facade()
        self._batch_size = max(1, batch_size)
        self._receive_timeout = max(0.001, receive_timeout)
        self._after_commit_hook = after_commit_hook
        self._stop_requested = asyncio.Event()
        self._activation = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._runtime_handle: RuntimeHandle | None = None
        self._state = ProjectMapAgentState.NEW
        self.degraded = False

    @property
    def state(self) -> ProjectMapAgentState:
        return self._state

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def runtime_handle(self) -> RuntimeHandle | None:
        return self._runtime_handle

    async def run(self, handle: RuntimeHandle, host: AgentRuntimeHost) -> None:
        """Run as one task owned by the shared Runtime Host."""
        async with self._lifecycle_lock:
            if self._state is not ProjectMapAgentState.NEW:
                raise RuntimeError(f"project_map_handler_cannot_run:{self._state}")
            self._state = ProjectMapAgentState.STARTING
            self._task = asyncio.current_task()
            self._runtime_handle = handle
        try:
            await self._activation.wait()
            if self._stop_requested.is_set():
                return
            self._state = ProjectMapAgentState.RUNNING
            self._log("map.runtime_created", "completed")
            await self._consume()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state = ProjectMapAgentState.FAILED
            await self._mark_degraded(type(exc).__name__)
            with suppress(Exception):
                await host.transition(handle, RuntimeState.FAILED, reason="map_handler_failed")
            raise
        finally:
            await self.mailbox.close()
            if self._state not in {ProjectMapAgentState.FAILED, ProjectMapAgentState.STOPPING}:
                self._state = ProjectMapAgentState.STOPPED

    def activate(self) -> None:
        self._activation.set()

    async def stop(self) -> ProjectMapAgentState:
        async with self._lifecycle_lock:
            if self._state is ProjectMapAgentState.STOPPED:
                return self._state
            self._state = ProjectMapAgentState.STOPPING
            self._stop_requested.set()
            self.mailbox.notify(target=self.target)
            if self._task is not None and self._task is not asyncio.current_task():
                await asyncio.gather(self._task, return_exceptions=True)
            await self.mailbox.close()
            self._state = ProjectMapAgentState.STOPPED
            return self._state

    async def _consume(self) -> None:
        while not self._stop_requested.is_set():
            first = await self.mailbox.receive(self.target, timeout=self._receive_timeout)
            if first.status == "claimed":
                claims = [first]
                while len(claims) < self._batch_size:
                    next_claim = self.mailbox.claim(self.target)
                    if next_claim.status != "claimed":
                        break
                    claims.append(next_claim)
                await self._process_batch(claims)
                continue
            if first.status == "closed":
                return
            if first.status != "empty":
                await asyncio.sleep(self._receive_timeout)
                continue
            version = self.mailbox.wake_version
            self._log("map.empty_checked", "empty")
            if await self._retire_callback(self.project_id, self.generation, version):
                return

    async def _process_batch(self, claims: list[MailboxResult]) -> None:
        first_message_id = claims[0].message_id
        self._log("map.batch_claimed", "completed", message_id=first_message_id)
        messages: list[tuple[str, list[str]]] = []
        for claim in claims:
            envelope = claim.envelope
            payload = None if envelope is None else envelope.payload
            path = payload.get("path") if isinstance(payload, dict) else None
            if envelope is None or envelope.message_type != "CodeChanged" or not isinstance(path, str):
                await self._mark_degraded("unsupported_map_message", message_id=claim.message_id)
                self._nack_claims(claims)
                return
            assert claim.message_id is not None
            messages.append((claim.message_id, [path]))
        self._log("map.refresh_started", "started", message_id=first_message_id)
        store = ProjectPlanStore(
            self.canonical_path,
            project_id=self.project_id,
            facade=self._logging,
        )
        try:
            await asyncio.to_thread(store.initialize, scan_if_created=False)
            result = await asyncio.to_thread(store.apply_code_changed_batch, messages)
        except Exception as exc:
            await self._mark_degraded(type(exc).__name__, message_id=first_message_id)
            self._nack_claims(claims)
            return
        self._log("map.transaction_committed", "completed", message_id=first_message_id)
        for message_id in result["duplicate_message_ids"]:
            self._log("map.message_duplicate", "ignored", message_id=message_id)
        if self._after_commit_hook is not None:
            self._after_commit_hook()
        acked = True
        for claim in claims:
            assert claim.message_id is not None and claim.lease_token is not None
            outcome = self.mailbox.ack(
                claim.message_id,
                claim.lease_token,
                target=self.target,
            )
            acked = acked and outcome.status == "acked"
        if acked:
            self._log("map.batch_acked", "completed", message_id=first_message_id)
        else:
            await self._mark_degraded("mail_ack_lost_lease", message_id=first_message_id)

    def _nack_claims(self, claims: list[MailboxResult]) -> None:
        for claim in claims:
            if claim.message_id is None or claim.lease_token is None:
                continue
            self.mailbox.nack(claim.message_id, claim.lease_token, target=self.target)

    async def _mark_degraded(self, reason: str, *, message_id: str | None = None) -> None:
        self.degraded = True
        try:
            store = ProjectPlanStore(
                self.canonical_path,
                project_id=self.project_id,
                facade=self._logging,
            )
            await asyncio.to_thread(store.mark_map_degraded, reason=reason)
        except Exception as exc:
            self._log(
                "map.degraded_persist_failed",
                "failed",
                message_id=message_id,
                error_code=type(exc).__name__,
            )
        self._log("map.degraded", "degraded", message_id=message_id, error_code=reason)

    def _log(
        self,
        action: str,
        status: str,
        *,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self._logging.info_event(
            action,
            status,
            trace_id=f"map-{self.project_id}-{self.generation}",
            message_id=message_id or f"map-runtime-{self.project_id}-{self.generation}",
            project_id=self.project_id,
            agent_id="map-runtime",
            generation=self.generation,
            error_code=error_code,
            detail={"state": self._state},
        )
