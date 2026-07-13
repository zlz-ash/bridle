"""FastAPI app factory."""
from __future__ import annotations

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
        try:
            yield
        finally:
            result = await runtime_registry.stop_all()
            if result.failures:
                get_logging_facade().error_event(
                    "project_runtime_shutdown",
                    "failed",
                    error_code="project_runtime_shutdown_failed",
                    detail={
                        "failure_count": len(result.failures),
                        "project_ids": [failure.project_id for failure in result.failures],
                    },
                )
                raise ProjectRuntimeShutdownError(
                    "one or more project runtimes failed to stop",
                    failures=result.failures,
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

