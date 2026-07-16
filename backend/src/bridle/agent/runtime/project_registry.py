from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
from bridle.agent.runtime.authorization import (
    AgentAuthorizationService,
    AgentIdentity,
    AgentRole,
)
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.mailbox import AgentAddress
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_map_agent import (
    ProjectMapAgent,
    ProjectRuntimeShutdownError,
)
from bridle.api.errors import ConflictError
from bridle.logging.facade import LoggingFacade, get_logging_facade


@dataclass(frozen=True)
class ProjectRuntimeStopFailure:
    project_id: str
    error_code: str
    error_type: str


@dataclass(frozen=True)
class ProjectRuntimeStopAllResult:
    failures: tuple[ProjectRuntimeStopFailure, ...] = ()


RetireHook = Callable[[str, str], Awaitable[None] | None]
MapAgentFactory = Callable[..., ProjectMapAgent]


class ProjectRuntimeRegistry:
    """Own at most one on-demand Map Runtime generation per project."""

    def __init__(
        self,
        *,
        logging_facade: LoggingFacade | None = None,
        retire_hook: RetireHook | None = None,
        runtime_host: AgentRuntimeHost | None = None,
        agent_factory: MapAgentFactory | None = None,
    ) -> None:
        self._logging = logging_facade or get_logging_facade()
        self._retire_hook = retire_hook
        self._runtime_host = runtime_host
        self._agent_factory = agent_factory or ProjectMapAgent
        self._lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._by_project: dict[str, ProjectMapAgent] = {}
        self._project_by_path: dict[str, str] = {}
        self._generations: dict[str, int] = {}
        self._retiring: set[str] = set()
        self._finalizer_tasks: set[asyncio.Task[None]] = set()
        self._finalizer_failures: list[ProjectRuntimeStopFailure] = []

    @property
    def active_count(self) -> int:
        return len(self._by_project)

    @property
    def active_project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_project))

    def get(self, project_id: str) -> ProjectMapAgent:
        return self._by_project[project_id]

    def generation(self, project_id: str) -> int:
        return self._generations.get(project_id, 0)

    async def ensure_started(
        self,
        project_id: str,
        project_root: str | Path,
    ) -> ProjectMapAgent:
        return await self.wake(project_id, project_root)

    async def wake(
        self,
        project_id: str,
        project_root: str | Path,
    ) -> ProjectMapAgent:
        canonical = Path(project_root).expanduser().resolve()
        path_key = self._path_key(canonical)
        async with self._lock:
            if self._shutting_down:
                raise ProjectRuntimeShutdownError(
                    "project runtime registry is shutting down",
                    error_code="project_runtime_registry_shutting_down",
                )
            self._validate_identity(project_id, path_key)
            current = self._by_project.get(project_id)
            if (
                current is not None
                and current.runtime_handle is not None
                and current.runtime_handle.state is RuntimeState.RUNNING
                and current.task is not None
                and not current.task.done()
            ):
                self._retiring.discard(project_id)
                self._log("map.wake_requested", "reused", current)
                return current
            if current is not None:
                self._remove_locked(project_id, current)
                await self._destroy_runtime(current)
            generation = self._generations.get(project_id, 0) + 1
            self._generations[project_id] = generation
            target = AgentAddress(project_id, "map-runtime", 1)
            mailbox = PersistentMailbox(
                canonical / ".bridle" / "mail.db",
                project_id=project_id,
                consumer_id=f"map-runtime-{generation}",
                default_target=target,
                facade=self._logging,
                trace_id=f"map-{project_id}-{generation}",
            )
            agent = self._agent_factory(
                project_id,
                canonical,
                generation=generation,
                mailbox=mailbox,
                retire_callback=self.retire_if_idle,
                logging_facade=self._logging,
            )
            host = self._host()
            await self._destroy_stale_host_runtime(host, project_id)
            handle = None
            try:
                handle = await host.create_runtime(
                    role=RuntimeRole.MAP,
                    project_id=project_id,
                    agent_id=f"map-runtime-{project_id}",
                    generation=generation,
                    grant=self._map_grant(project_id),
                    task_factory=lambda runtime_handle: agent.run(runtime_handle, host),
                    mailbox=mailbox,
                )
                await host.transition(
                    handle,
                    RuntimeState.RUNNING,
                    reason="map_handler_started",
                )
                self._by_project[project_id] = agent
                self._project_by_path[path_key] = project_id
                agent.activate()
            except BaseException:
                self._remove_locked(project_id, agent)
                if handle is not None:
                    with suppress(Exception):
                        await host.destroy(handle)
                else:
                    with suppress(Exception):
                        await mailbox.close()
                raise
            self._log("map.wake_requested", "created", agent)
            return agent

    async def wake_if_pending(
        self,
        project_id: str,
        project_root: str | Path,
    ) -> ProjectMapAgent | None:
        canonical = Path(project_root).expanduser().resolve()
        target = AgentAddress(project_id, "map-runtime", 1)
        probe = PersistentMailbox(
            canonical / ".bridle" / "mail.db",
            project_id=project_id,
            consumer_id="map-runtime-pending-probe",
            default_target=target,
            facade=self._logging,
        )
        try:
            if not probe.has_pending(target):
                return None
        finally:
            await probe.close()
        return await self.wake(project_id, canonical)

    async def retire_if_idle(
        self,
        project_id: str,
        generation: int,
        expected_version: int,
    ) -> bool:
        async with self._lock:
            current = self._by_project.get(project_id)
            if current is None or current.generation != generation:
                return True
            self._retiring.add(project_id)
            await self._call_retire_hook(project_id, "after_first_empty")
            if not current.mailbox.is_empty_at_version(current.target, expected_version):
                self._retiring.discard(project_id)
                return False
            await self._call_retire_hook(project_id, "after_second_empty")
            if not current.mailbox.is_empty_at_version(current.target, expected_version):
                self._retiring.discard(project_id)
                return False
            self._remove_locked(project_id, current)
            self._retiring.discard(project_id)
            self._log("map.runtime_destroyed", "completed", current)
            await self._call_retire_hook(project_id, "after_removal")
            finalizer = asyncio.create_task(self._destroy_runtime(current))
            self._finalizer_tasks.add(finalizer)
            finalizer.add_done_callback(
                lambda task, agent=current: self._record_retired_finalizer(
                    agent,
                    task,
                )
            )
            return True

    async def begin_shutdown(self) -> None:
        """Permanently close admission before producers and runtimes are drained."""
        async with self._lock:
            self._shutting_down = True

    async def stop(self, project_id: str) -> None:
        async with self._lock:
            current = self._by_project.get(project_id)
            if current is None:
                return
            self._remove_locked(project_id, current)
            self._retiring.add(project_id)
        try:
            await self._destroy_runtime(current)
            self._log("map.runtime_destroyed", "completed", current)
        finally:
            async with self._lock:
                self._retiring.discard(project_id)

    async def stop_all(self) -> ProjectRuntimeStopAllResult:
        async with self._shutdown_lock:
            await self.begin_shutdown()
            failures: list[ProjectRuntimeStopFailure] = []
            for project_id in tuple(self.active_project_ids):
                try:
                    await self.stop(project_id)
                except Exception as exc:
                    failures.append(
                        ProjectRuntimeStopFailure(
                            project_id,
                            "project_runtime_stop_failed",
                            type(exc).__name__,
                        )
                    )
            while self._finalizer_tasks:
                pending = tuple(self._finalizer_tasks)
                await asyncio.gather(*pending, return_exceptions=True)
            failures.extend(self._finalizer_failures)
            self._finalizer_failures.clear()
            host = self._runtime_host
            if host is not None:
                for handle in tuple(host.active_handles()):
                    if handle.spec.role is not RuntimeRole.MAP:
                        continue
                    try:
                        await host.destroy(handle)
                    except Exception as exc:
                        failures.append(
                            ProjectRuntimeStopFailure(
                                handle.spec.project_id,
                                "project_runtime_host_retry_failed",
                                type(exc).__name__,
                            )
                        )
            return ProjectRuntimeStopAllResult(tuple(failures))

    def _record_retired_finalizer(
        self,
        agent: ProjectMapAgent,
        task: asyncio.Task[None],
    ) -> None:
        self._finalizer_tasks.discard(task)
        if task.cancelled():
            error_type = "CancelledError"
        else:
            error = task.exception()
            if error is None:
                return
            error_type = type(error).__name__
        self._finalizer_failures.append(
            ProjectRuntimeStopFailure(
                agent.project_id,
                "project_runtime_finalizer_failed",
                error_type,
            )
        )
        self._logging.error_event(
            "map.runtime_destroy_failed",
            "failed",
            trace_id=f"map-{agent.project_id}-{agent.generation}",
            message_id=f"map-runtime-{agent.project_id}-{agent.generation}",
            project_id=agent.project_id,
            agent_id="map-runtime",
            generation=agent.generation,
            detail={"reason": "project_runtime_finalizer_failed"},
        )

    async def _call_retire_hook(self, project_id: str, stage: str) -> None:
        if self._retire_hook is None:
            return
        result = self._retire_hook(project_id, stage)
        if inspect.isawaitable(result):
            await result

    def _host(self) -> AgentRuntimeHost:
        if self._runtime_host is None:
            from bridle import database

            database._ensure_engine()
            assert database.async_session is not None
            self._runtime_host = AgentRuntimeHost(
                database.async_session,
                facade=self._logging,
            )
        return self._runtime_host

    @staticmethod
    def _map_grant(project_id: str):
        return AgentAuthorizationService().resolve(
            identity=AgentIdentity(
                principal_id="map-runtime",
                role=AgentRole.PROJECT_MAPPER,
                project_id=project_id,
                agent_id="map-runtime",
            ),
            policy_version="runtime-v1",
        )

    async def _destroy_runtime(self, agent: ProjectMapAgent) -> None:
        handle = agent.runtime_handle
        if handle is None:
            await agent.stop()
            return
        await self._host().destroy(handle)

    @staticmethod
    async def _destroy_stale_host_runtime(
        host: AgentRuntimeHost,
        project_id: str,
    ) -> None:
        for handle in host.active_handles():
            if (
                handle.spec.role is RuntimeRole.MAP
                and handle.spec.project_id == project_id
                and handle.state is not RuntimeState.DESTROYED
            ):
                await host.destroy(handle)

    def _validate_identity(self, project_id: str, path_key: str) -> None:
        current = self._by_project.get(project_id)
        if current is not None and self._path_key(current.canonical_path) != path_key:
            self._raise_identity_conflict(project_id)
        owner = self._project_by_path.get(path_key)
        if owner is not None and owner != project_id:
            self._raise_identity_conflict(project_id)

    def _remove_locked(self, project_id: str, agent: ProjectMapAgent) -> None:
        if self._by_project.get(project_id) is agent:
            self._by_project.pop(project_id, None)
            self._project_by_path.pop(self._path_key(agent.canonical_path), None)

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(str(path))

    def _raise_identity_conflict(self, project_id: str) -> None:
        raise ConflictError(
            resource="project_runtime",
            message="Project runtime identity conflicts with an active project",
            error_code="project_runtime_identity_conflict",
            details={"project_id": project_id},
        )

    def _log(self, action: str, status: str, agent: ProjectMapAgent) -> None:
        self._logging.info_event(
            action,
            status,
            trace_id=f"map-{agent.project_id}-{agent.generation}",
            message_id=f"map-runtime-{agent.project_id}-{agent.generation}",
            project_id=agent.project_id,
            agent_id="map-runtime",
            generation=agent.generation,
            detail={"active_count": self.active_count},
        )


_registry: ProjectRuntimeRegistry | None = None


def get_project_runtime_registry() -> ProjectRuntimeRegistry:
    global _registry
    if _registry is None:
        _registry = ProjectRuntimeRegistry()
    return _registry


def configure_project_runtime_registry(registry: ProjectRuntimeRegistry) -> None:
    global _registry
    _registry = registry


def reset_project_runtime_registry_for_tests() -> None:
    global _registry
    _registry = None
