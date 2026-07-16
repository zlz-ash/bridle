"""FastAPI app factory."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from bridle import __version__
from bridle.api.errors import BridleError
from bridle.events.bus import EventBus
from bridle.features.project_map.controller import router as project_map_router
from bridle.features.projects.controller import router as projects_router
from bridle.features.sessions.controller import router as project_sessions_router
from bridle.features.system.events_controller import router as events_router
from bridle.features.system.health_controller import router as health_router
from bridle.features.workspace.files_controller import router as workspace_files_router

logger = logging.getLogger("bridle")


def create_app(
    test_db=None,
    test_workspace: str | None = None,
    container_runner=None,
    project_runtime_registry=None,
    runtime_lifecycle=None,
    runtime_shutdown_timeout: float = 5.0,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        test_db: If provided, overrides the DB dependency for testing.
        test_workspace: If provided, sets workspace for testing.
        container_runner: Optional explicit container runner for tests or dry-run.
    """
    if test_workspace is not None:
        from bridle.config import set_workspace

        set_workspace(test_workspace)
        if container_runner is not None:
            from bridle.agent.container.container_service import configure_runner

            configure_runner(test_workspace, container_runner)

    if test_db is not None:
        from bridle.api.deps import set_test_db
        set_test_db(test_db)

    from bridle.agent.runtime.project_map_agent import ProjectRuntimeShutdownError
    from bridle.agent.runtime.project_registry import (
        ProjectRuntimeStopAllResult,
        ProjectRuntimeStopFailure,
        configure_project_runtime_registry,
        get_project_runtime_registry,
    )
    from bridle.logging.facade import get_logging_facade

    runtime_registry = project_runtime_registry or get_project_runtime_registry()
    if project_runtime_registry is not None:
        configure_project_runtime_registry(project_runtime_registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Run the application lifetime."""
        startup_mailboxes = []
        relay_stop = None
        relay_task = None
        application_sessions = None
        try:
            active_lifecycle = runtime_lifecycle
            if (
                active_lifecycle is None
                and test_db is None
                and test_workspace is None
                and project_runtime_registry is None
            ):
                from pathlib import Path

                from sqlalchemy import select

                from bridle import database
                from bridle.agent.runtime.input_relay import RuntimeInputRelay
                from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
                from bridle.agent.runtime.session_runtime_lifecycle import RuntimeSessionLifecycle
                from bridle.models.project import ProjectRecord

                database._ensure_engine()
                async with database.async_session() as session:
                    projects = (await session.execute(select(ProjectRecord))).scalars().all()
                project_paths = {project.id: Path(project.path) for project in projects}
                mailboxes: dict[str, PersistentMailbox] = {}

                def mailbox_for_project(project_id: str) -> PersistentMailbox:
                    current = mailboxes.get(project_id)
                    if current is not None:
                        return current
                    project_root = project_paths[project_id]
                    if not project_root.is_dir():
                        raise FileNotFoundError("project_path_unavailable")
                    current = PersistentMailbox(
                        project_root / ".bridle" / "mail.db",
                        project_id=project_id,
                        consumer_id="startup-recovery",
                    )
                    mailboxes[project_id] = current
                    startup_mailboxes.append(current)
                    return current

                active_lifecycle = RuntimeSessionLifecycle(
                    database.async_session,
                    relay=RuntimeInputRelay(
                        database.async_session,
                        mailbox_for_project=mailbox_for_project,
                    ),
                )
            if active_lifecycle is not None:
                application_sessions = active_lifecycle.sessions
            elif test_db is not None and test_db.bind is not None:
                from sqlalchemy.ext.asyncio import async_sessionmaker

                application_sessions = async_sessionmaker(
                    test_db.bind,
                    expire_on_commit=False,
                )
            if application_sessions is not None:
                from sqlalchemy import delete, select

                from bridle.agent.runtime.gateway import recover_project_runtime
                from bridle.models.project import ProjectRecord
                from bridle.models.project_runtime_recovery import (
                    ProjectRuntimeRecoveryRecord,
                )

                bind = application_sessions.kw["bind"]
                async with bind.begin() as connection:
                    await connection.run_sync(
                        ProjectRuntimeRecoveryRecord.__table__.create,
                        checkfirst=True,
                    )
                if active_lifecycle is not None:
                    await active_lifecycle.recover_before_requests()
                async with application_sessions() as session:
                    projects = (
                        await session.execute(select(ProjectRecord))
                    ).scalars().all()
                for project in projects:
                    try:
                        await recover_project_runtime(
                            project_path=project.path,
                            project_id=project.id,
                            facade=get_logging_facade(),
                        )
                    except Exception as exc:
                        async with application_sessions() as session:
                            record = await session.get(
                                ProjectRuntimeRecoveryRecord,
                                project.id,
                            )
                            reason = f"runtime_recovery_{type(exc).__name__}"
                            if record is None:
                                session.add(
                                    ProjectRuntimeRecoveryRecord(
                                        project_id=project.id,
                                        status="degraded",
                                        reason=reason,
                                        error_type=type(exc).__name__,
                                    )
                                )
                            else:
                                record.status = "degraded"
                                record.reason = reason
                                record.error_type = type(exc).__name__
                            await session.commit()
                        get_logging_facade().error_event(
                            "app.runtime_project_degraded",
                            "degraded",
                            project_id=project.id,
                            error_code=type(exc).__name__,
                            detail={"reason": "project_recovery_failed"},
                        )
                    else:
                        async with application_sessions() as session:
                            await session.execute(
                                delete(ProjectRuntimeRecoveryRecord).where(
                                    ProjectRuntimeRecoveryRecord.project_id == project.id
                                )
                            )
                            await session.commit()
                        get_logging_facade().info_event(
                            "app.runtime_project_recovered",
                            "completed",
                            project_id=project.id,
                            detail={"recovered": True},
                        )
            if active_lifecycle is not None:
                relay_stop = asyncio.Event()
                relay_task = asyncio.create_task(
                    active_lifecycle.run_relay_retry(relay_stop),
                    name="runtime-input-relay",
                )
            yield
        finally:
            from bridle.agent.runtime.gateway import shutdown_gateway_runtimes

            get_logging_facade().info_event(
                "app.runtime_shutdown_started",
                "started",
                detail={"timeout_seconds": runtime_shutdown_timeout},
            )
            failures: list[ProjectRuntimeStopFailure] = []
            await runtime_registry.begin_shutdown()
            if relay_stop is not None:
                relay_stop.set()
            if relay_task is not None:
                try:
                    await relay_task
                except Exception as exc:
                    failures.append(
                        ProjectRuntimeStopFailure(
                            "runtime-input-relay",
                            "runtime_input_relay_stop_failed",
                            type(exc).__name__,
                        )
                    )
            shutdown_task = asyncio.create_task(shutdown_gateway_runtimes())
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(shutdown_task),
                    timeout=runtime_shutdown_timeout,
                )
            except TimeoutError:
                get_logging_facade().error_event(
                    "app.runtime_shutdown_forced",
                    "forced",
                    error_code="runtime_shutdown_timeout",
                    detail={"timeout_seconds": runtime_shutdown_timeout},
                )
                result = await shutdown_task
            except Exception as exc:
                failures.append(
                    ProjectRuntimeStopFailure(
                        "gateway",
                        "gateway_runtime_shutdown_failed",
                        type(exc).__name__,
                    )
                )
                result = ProjectRuntimeStopAllResult()
            for mailbox in startup_mailboxes:
                try:
                    await mailbox.close()
                except Exception as exc:
                    failures.append(
                        ProjectRuntimeStopFailure(
                            mailbox.project_id,
                            "startup_mailbox_close_failed",
                            type(exc).__name__,
                        )
                    )
            failures.extend(result.failures)
            if failures:
                get_logging_facade().error_event(
                    "app.runtime_shutdown_failed",
                    "failed",
                    error_code="project_runtime_shutdown_failed",
                    detail={
                        "failure_count": len(failures),
                    },
                )
                get_logging_facade().info_event(
                    "app.runtime_shutdown_completed",
                    "completed",
                    detail={"failure_count": len(failures)},
                )
                raise ProjectRuntimeShutdownError(
                    "one or more project runtimes failed to stop",
                    failures=tuple(failures),
                )
            get_logging_facade().info_event(
                "app.runtime_shutdown_completed",
                "completed",
                detail={"failure_count": 0},
            )

    app = FastAPI(title="Bridle", version=__version__, lifespan=lifespan)
    app.state.started_at = time.time()
    EventBus._reset_instance()

    # Only HTTP methods that mutate state produce traces. Pure reads (GET,
    # HEAD) and CORS preflight (OPTIONS) are dropped to keep Langfuse trace
    # volume bounded; SSE/poll endpoints would otherwise create thousands of
    # empty "project_session.request" traces per session.
    _OBSERVABLE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
    _NON_OBSERVABLE_PATH_PREFIXES = (
        "/health",
        "/docs",
        "/openapi",
        "/favicon",
        "/static",
    )

    def _should_trace(request: Request) -> bool:
        if request.method not in _OBSERVABLE_METHODS:
            return False
        path = request.url.path
        return not any(path.startswith(p) for p in _NON_OBSERVABLE_PATH_PREFIXES)

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):
        from bridle.observability import get_observability
        from bridle.observability.context import bind_log_context
        from bridle.observability.schema import ObservabilityContext

        session_id = request.headers.get("x-bridle-session-id")
        if session_id is None and "/sessions/" in request.url.path:
            parts = request.url.path.split("/sessions/", 1)[-1].split("/")
            if parts and parts[0]:
                session_id = parts[0]

        if not _should_trace(request):
            # Still propagate session_id into log context so any business code
            # downstream (and any explicit start_trace it issues) keeps its
            # logical session linkage; we only skip the HTTP-level root trace.
            ctx = ObservabilityContext(session_id=session_id)
            obs = get_observability()
            with obs.bind_context(ctx):
                bind_log_context(session_id=session_id)
                return await call_next(request)

        obs = get_observability()
        # ``trace_name`` becomes the Langfuse Trace Name column; ``session_id``
        # promotes the trace into the Langfuse Sessions view so the thousands
        # of per-request traces aggregate under one navigable session.
        trace = obs.start_trace(
            "project_session.request",
            session_id=session_id,
            trace_name=f"{request.method} {request.url.path}",
            path=request.url.path,
            method=request.method,
        )
        ctx = ObservabilityContext(session_id=session_id)
        with obs.bind_context(ctx):
            bind_log_context(session_id=session_id)
            try:
                response = await call_next(request)
                trace.end(status="completed")
                return response
            except Exception as exc:
                trace.end(status="failed", error_code=type(exc).__name__)
                raise

    @app.exception_handler(BridleError)
    async def bridle_error_handler(request: Request, exc: BridleError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.api_error.model_dump(exclude_none=True),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            errors.append({
                "loc": err.get("loc", []),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            })
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": errors},
            },
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception", extra={
            "action": "internal_error",
            "status": "error",
            "detail": str(exc),
        })
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "An unexpected error occurred",
                "details": {"error_type": type(exc).__name__},
            },
        )

    app.include_router(health_router, prefix="/api/v1")
    app.include_router(workspace_files_router, prefix="/api/v1")
    app.include_router(events_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(project_sessions_router, prefix="/api/v1")
    app.include_router(project_map_router, prefix="/api/v1")

    return app

