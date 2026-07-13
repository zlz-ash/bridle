from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from bridle.logging.facade import LoggingFacade, get_logging_facade


class ProjectMapAgentState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOP_FAILED = "stop_failed"
    STOPPED = "stopped"
    FAILED = "failed"


class ProjectMapWatcher(Protocol):
    def start(self, project_root: Path, *, project_id: str) -> None: ...

    def stop(self, project_id: str, *, timeout_seconds: float = 5.0) -> bool: ...


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


TaskFactory = Callable[..., asyncio.Task[None]]
_STOP = object()


class ProjectMapAgent:
    def __init__(
        self,
        project_id: str,
        project_root: str | Path,
        *,
        watcher: ProjectMapWatcher,
        task_factory: TaskFactory = asyncio.create_task,
        logging_facade: LoggingFacade | None = None,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        self.project_id = project_id
        self.canonical_path = Path(project_root).expanduser().resolve()
        self._watcher = watcher
        self._task_factory = task_factory
        self._logging = logging_facade or get_logging_facade()
        self._stop_timeout_seconds = stop_timeout_seconds
        self._mailbox: asyncio.Queue[object] = asyncio.Queue()
        self._lifecycle_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._actor_stop_requested = False
        self._actor_failure_reported = False
        self._state = ProjectMapAgentState.NEW

    @property
    def state(self) -> ProjectMapAgentState:
        return self._state

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def start(self) -> ProjectMapAgent:
        async with self._lifecycle_lock:
            if self._state is ProjectMapAgentState.RUNNING:
                return self
            if self._state is not ProjectMapAgentState.NEW:
                raise RuntimeError(f"project_map_agent_cannot_start:{self._state}")
            self._state = ProjectMapAgentState.STARTING
            watcher_start = asyncio.create_task(
                asyncio.to_thread(
                    self._watcher.start,
                    self.canonical_path,
                    project_id=self.project_id,
                )
            )
            cancelled = await self._wait_until_done(watcher_start)
            start_error: Exception | None = None
            try:
                watcher_start.result()
            except Exception as exc:
                start_error = exc

            if cancelled or start_error is not None:
                rollback_error, rollback_cancelled = await self._rollback_watcher()
                self._state = (
                    ProjectMapAgentState.STOP_FAILED
                    if rollback_error is not None
                    else ProjectMapAgentState.FAILED
                )
                self._log_start_failure(start_error, rollback_error)
                if rollback_error is not None:
                    raise ProjectRuntimeShutdownError(
                        "project map agent start rollback failed",
                        error_code="project_map_agent_start_rollback_failed",
                        cancelled=cancelled or rollback_cancelled,
                    ) from (start_error or asyncio.CancelledError())
                if cancelled or rollback_cancelled:
                    raise asyncio.CancelledError
                assert start_error is not None
                raise start_error

            try:
                coroutine: Coroutine[Any, Any, None] = self._run()
                try:
                    self._task = self._task_factory(
                        coroutine,
                        name=f"project-map-agent-{self.project_id[:8]}",
                    )
                except Exception:
                    coroutine.close()
                    raise
                self._state = ProjectMapAgentState.RUNNING
                self._logging.info_event(
                    "project_map_agent_start",
                    "completed",
                    detail={"project_id": self.project_id, "state": self._state},
                )
                return self
            except Exception as exc:
                if self._task is not None and not self._task.done():
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
                self._task = None
                rollback_error, rollback_cancelled = await self._rollback_watcher()
                self._state = (
                    ProjectMapAgentState.STOP_FAILED
                    if rollback_error is not None
                    else ProjectMapAgentState.FAILED
                )
                self._log_start_failure(exc, rollback_error)
                if rollback_error is not None:
                    raise ProjectRuntimeShutdownError(
                        "project map agent start rollback failed",
                        error_code="project_map_agent_start_rollback_failed",
                        cancelled=rollback_cancelled,
                    ) from exc
                if rollback_cancelled:
                    raise asyncio.CancelledError from exc
                raise

    async def stop(self) -> ProjectMapAgentState:
        async with self._lifecycle_lock:
            if self._state is ProjectMapAgentState.STOPPED:
                return self._state
            if self._state in {ProjectMapAgentState.NEW, ProjectMapAgentState.FAILED}:
                self._state = ProjectMapAgentState.STOPPED
                return self._state

            cleanup = asyncio.create_task(self._stop_impl())
            cancelled = await self._wait_until_done(cleanup)
            cleanup_error: BaseException | None = None
            result: ProjectMapAgentState | None = None
            try:
                result = cleanup.result()
            except BaseException as exc:
                cleanup_error = exc
            if cleanup_error is not None:
                if cancelled and isinstance(cleanup_error, ProjectRuntimeShutdownError):
                    cleanup_error.cancelled = True
                raise cleanup_error
            if cancelled:
                raise asyncio.CancelledError
            assert result is not None
            return result

    async def _stop_impl(self) -> ProjectMapAgentState:
        self._state = ProjectMapAgentState.STOPPING
        errors: list[BaseException] = []
        watcher_error: Exception | None = None
        if self._task is not None:
            if not self._task.done():
                self._actor_stop_requested = True
                await self._mailbox.put(_STOP)
                try:
                    await self._task
                except asyncio.CancelledError as exc:
                    self._actor_failure_reported = True
                    errors.append(exc)
                except Exception as exc:  # pragma: no cover - defensive task failure path
                    self._actor_failure_reported = True
                    errors.append(exc)
            elif not self._actor_stop_requested and not self._actor_failure_reported:
                self._actor_stop_requested = True
                self._actor_failure_reported = True
                try:
                    self._task.result()
                except asyncio.CancelledError as exc:
                    errors.append(exc)
                except Exception as exc:  # pragma: no cover - defensive task failure path
                    errors.append(exc)
                else:
                    errors.append(RuntimeError("project_map_agent_task_exited"))
        try:
            stopped = await asyncio.to_thread(
                self._watcher.stop,
                self.project_id,
                timeout_seconds=self._stop_timeout_seconds,
            )
            if not stopped:
                watcher_error = RuntimeError("map_watcher_stop_timeout")
        except Exception as exc:
            watcher_error = exc
        if watcher_error is not None:
            errors.append(watcher_error)
        if errors:
            primary_error = watcher_error or errors[0]
            self._state = ProjectMapAgentState.STOP_FAILED
            self._logging.error_event(
                "project_map_agent_stop",
                "failed",
                error_code="project_map_agent_stop_failed",
                detail={
                    "project_id": self.project_id,
                    "error_type": type(primary_error).__name__,
                },
            )
            raise ProjectRuntimeShutdownError(
                "project map agent cleanup failed",
                error_code="project_map_agent_stop_failed",
            ) from primary_error
        self._state = ProjectMapAgentState.STOPPED
        self._logging.info_event(
            "project_map_agent_stop",
            "completed",
            detail={"project_id": self.project_id, "state": self._state},
        )
        return self._state

    async def _rollback_watcher(self) -> tuple[Exception | None, bool]:
        rollback = asyncio.create_task(
            asyncio.to_thread(
                self._watcher.stop,
                self.project_id,
                timeout_seconds=self._stop_timeout_seconds,
            )
        )
        cancelled = await self._wait_until_done(rollback)
        try:
            stopped = rollback.result()
            if stopped:
                return None, cancelled
            return RuntimeError("map_watcher_stop_timeout"), cancelled
        except Exception as exc:
            return exc, cancelled

    async def _wait_until_done(self, task: asyncio.Task[Any]) -> bool:
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                cancelled = True
        return cancelled

    def _log_start_failure(
        self,
        start_error: Exception | None,
        rollback_error: Exception | None,
    ) -> None:
        if rollback_error is not None:
            self._logging.error_event(
                "project_map_agent_start_rollback",
                "failed",
                error_code="project_map_agent_start_rollback_failed",
                detail={
                    "project_id": self.project_id,
                    "error_type": type(rollback_error).__name__,
                },
            )
        self._logging.error_event(
            "project_map_agent_start",
            "failed",
            error_code="project_map_agent_start_failed",
            detail={
                "project_id": self.project_id,
                "error_type": type(start_error).__name__ if start_error else "CancelledError",
            },
        )

    async def _run(self) -> None:
        while True:
            message = await self._mailbox.get()
            if message is _STOP:
                return
