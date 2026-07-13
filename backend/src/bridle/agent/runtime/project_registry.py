from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from bridle.agent.runtime.project_map_agent import (
    ProjectMapAgent,
    ProjectMapAgentState,
    ProjectMapWatcher,
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


AgentFactory = Callable[[str, Path], ProjectMapAgent]
T = TypeVar("T")


class ProjectRuntimeRegistry:
    def __init__(
        self,
        *,
        watcher: ProjectMapWatcher | None = None,
        agent_factory: AgentFactory | None = None,
        logging_facade: LoggingFacade | None = None,
    ) -> None:
        if watcher is None:
            from bridle.features.project_map.watcher import get_code_map_watcher

            watcher = get_code_map_watcher()
        self._watcher = watcher
        self._logging = logging_facade or get_logging_facade()
        self._agent_factory = agent_factory or self._new_agent
        self._lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._by_project: dict[str, ProjectMapAgent] = {}
        self._project_by_path: dict[str, str] = {}
        self._starting: dict[str, asyncio.Task[ProjectMapAgent]] = {}
        self._stopping: dict[str, asyncio.Task[None]] = {}

    @property
    def active_count(self) -> int:
        return len(self._by_project)

    @property
    def active_project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_project))

    def get(self, project_id: str) -> ProjectMapAgent:
        return self._by_project[project_id]

    async def ensure_started(
        self,
        project_id: str,
        project_root: str | Path,
    ) -> ProjectMapAgent:
        canonical = Path(project_root).expanduser().resolve()
        path_key = self._path_key(canonical)
        while True:
            cleanup_failed_owner = False
            wait_for_stop: asyncio.Task[None] | None = None
            async with self._lock:
                if self._shutting_down:
                    self._logging.warn_event(
                        "project_runtime_ensure",
                        "rejected",
                        error_code="project_runtime_registry_shutting_down",
                        detail={"project_id": project_id},
                    )
                    raise ProjectRuntimeShutdownError(
                        "project runtime registry is shutting down",
                        error_code="project_runtime_registry_shutting_down",
                    )
                wait_for_stop = self._stopping.get(project_id)
                current = self._by_project.get(project_id)
                if (
                    current is not None
                    and self._path_key(current.canonical_path) != path_key
                ):
                    self._raise_identity_conflict(project_id)
                if wait_for_stop is None and current is not None:
                    if (
                        current.state is not ProjectMapAgentState.RUNNING
                        or current.task is None
                        or current.task.done()
                    ):
                        cleanup_failed_owner = True
                    else:
                        self._logging.info_event(
                            "project_runtime_ensure",
                            "reused",
                            detail={"project_id": project_id},
                        )
                        return current
                starting = self._starting.get(project_id)
                if wait_for_stop is None and not cleanup_failed_owner and starting is None:
                    path_owner = self._project_by_path.get(path_key)
                    if path_owner is not None and path_owner != project_id:
                        self._raise_identity_conflict(project_id)
                    agent = self._agent_factory(project_id, canonical)
                    starting = asyncio.create_task(
                        self._start_and_commit(project_id, path_key, agent),
                        name=f"project-runtime-start-{project_id[:8]}",
                    )
                    self._starting[project_id] = starting
                    self._project_by_path[path_key] = project_id
                elif starting is not None:
                    if self._project_by_path.get(path_key) != project_id:
                        self._raise_identity_conflict(project_id)

            if wait_for_stop is not None:
                await self._await_shared_task(wait_for_stop)
                continue
            if cleanup_failed_owner:
                await self.stop(project_id)
                continue
            assert starting is not None
            return await self._await_shared_task(starting)

    async def stop(self, project_id: str) -> None:
        operation = asyncio.create_task(
            self._stop_until_removed(project_id),
            name=f"project-runtime-stop-request-{project_id[:8]}",
        )
        await self._await_shared_task(operation)

    async def _stop_until_removed(self, project_id: str) -> None:
        while True:
            starting: asyncio.Task[ProjectMapAgent] | None = None
            async with self._lock:
                stopping = self._stopping.get(project_id)
                if stopping is None:
                    agent = self._by_project.get(project_id)
                    if agent is None:
                        starting = self._starting.get(project_id)
                        if starting is None:
                            return
                    else:
                        stopping = asyncio.create_task(
                            self._stop_and_commit(project_id, agent),
                            name=f"project-runtime-stop-{project_id[:8]}",
                        )
                        self._stopping[project_id] = stopping
            if starting is not None:
                with suppress(BaseException):
                    await starting
                continue
            assert stopping is not None
            await stopping
            return

    async def stop_all(self) -> ProjectRuntimeStopAllResult:
        async with self._shutdown_lock:
            async with self._lock:
                self._shutting_down = True
                project_ids = tuple(sorted(set(self._by_project) | set(self._starting)))
            cleanup = asyncio.create_task(
                self._stop_all_projects(project_ids),
                name="project-runtime-stop-all",
            )
            cancelled = await self._wait_until_done(cleanup)
            cleanup_error: BaseException | None = None
            result: ProjectRuntimeStopAllResult | None = None
            try:
                result = cleanup.result()
            except BaseException as exc:
                cleanup_error = exc
            finally:
                self._shutting_down = False

            if cleanup_error is not None:
                if cancelled and isinstance(cleanup_error, ProjectRuntimeShutdownError):
                    cleanup_error.cancelled = True
                self._log_stop_all(project_ids, (), status="failed")
                raise cleanup_error
            assert result is not None
            if cancelled and result.failures:
                self._log_stop_all(project_ids, result.failures, status="failed")
                raise ProjectRuntimeShutdownError(
                    "project runtime shutdown failed after cancellation",
                    failures=result.failures,
                    cancelled=True,
                )
            if cancelled:
                self._log_stop_all(project_ids, (), status="cancelled")
                raise asyncio.CancelledError
            self._log_stop_all(
                project_ids,
                result.failures,
                status="failed" if result.failures else "completed",
            )
            return result

    async def _stop_all_projects(
        self,
        project_ids: tuple[str, ...],
    ) -> ProjectRuntimeStopAllResult:
        failed: list[str] = []
        for project_id in project_ids:
            try:
                await self.stop(project_id)
            except Exception:
                failed.append(project_id)
        persistent: list[ProjectRuntimeStopFailure] = []
        for project_id in failed:
            try:
                await self.stop(project_id)
            except Exception as exc:
                persistent.append(
                    ProjectRuntimeStopFailure(
                        project_id=project_id,
                        error_code="project_runtime_stop_failed",
                        error_type=type(exc).__name__,
                    )
                )
        return ProjectRuntimeStopAllResult(tuple(persistent))

    async def _wait_until_done(self, task: asyncio.Task[object]) -> bool:
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                cancelled = True
        return cancelled

    async def _await_shared_task(self, task: asyncio.Task[T]) -> T:
        cancelled = await self._wait_until_done(task)
        try:
            result = task.result()
        except BaseException as exc:
            if cancelled and isinstance(exc, ProjectRuntimeShutdownError):
                raise ProjectRuntimeShutdownError(
                    str(exc),
                    error_code=exc.error_code,
                    failures=exc.failures,
                    cancelled=True,
                ) from exc
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _log_stop_all(
        self,
        project_ids: tuple[str, ...],
        failures: tuple[ProjectRuntimeStopFailure, ...],
        *,
        status: str,
    ) -> None:
        self._logging.info_event(
            "project_runtime_stop_all",
            status,
            error_code=(
                "project_runtime_shutdown_failed"
                if failures or status == "failed"
                else "project_runtime_shutdown_cancelled"
                if status == "cancelled"
                else None
            ),
            detail={
                "project_count": len(project_ids),
                "failure_count": len(failures),
                "failed_project_ids": [item.project_id for item in failures],
            },
        )

    async def _start_and_commit(
        self,
        project_id: str,
        path_key: str,
        agent: ProjectMapAgent,
    ) -> ProjectMapAgent:
        try:
            await agent.start()
        except BaseException:
            async with self._lock:
                if agent.state is ProjectMapAgentState.STOP_FAILED:
                    self._by_project[project_id] = agent
                elif self._project_by_path.get(path_key) == project_id:
                    self._project_by_path.pop(path_key, None)
                self._starting.pop(project_id, None)
            self._logging.error_event(
                "project_runtime_ensure",
                "failed",
                error_code="project_runtime_start_failed",
                detail={"project_id": project_id, "state": agent.state},
            )
            raise
        async with self._lock:
            self._by_project[project_id] = agent
            self._starting.pop(project_id, None)
        return agent

    async def _stop_and_commit(self, project_id: str, agent: ProjectMapAgent) -> None:
        try:
            await agent.stop()
        except BaseException:
            async with self._lock:
                self._stopping.pop(project_id, None)
            raise
        async with self._lock:
            if self._by_project.get(project_id) is agent:
                self._by_project.pop(project_id, None)
                self._project_by_path.pop(self._path_key(agent.canonical_path), None)
            self._stopping.pop(project_id, None)

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(str(path))

    def _new_agent(self, project_id: str, canonical: Path) -> ProjectMapAgent:
        return ProjectMapAgent(
            project_id,
            canonical,
            watcher=self._watcher,
            logging_facade=self._logging,
        )

    def _raise_identity_conflict(self, project_id: str) -> None:
        self._logging.warn_event(
            "project_runtime_ensure",
            "rejected",
            error_code="project_runtime_identity_conflict",
            detail={"project_id": project_id},
        )
        raise ConflictError(
            resource="project_runtime",
            message="Project runtime identity conflicts with an active project",
            error_code="project_runtime_identity_conflict",
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
