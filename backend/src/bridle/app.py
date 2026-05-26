"""FastAPI app factory."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from bridle.api.errors import BridleError
from bridle.api.coding_sessions import router as coding_sessions_router
from bridle.api.node_agent_runs import router as node_agent_runs_router
from bridle.api.nodes import router as nodes_router
from bridle.api.plan_change_proposals import router as plan_change_proposals_router
from bridle.api.plans import router as plans_router
from bridle.api.proposals import router as proposals_router
from bridle.api.reports import router as reports_router
from bridle.api.tasks import router as tasks_router

logger = logging.getLogger("bridle")


def create_app(test_db=None, test_workspace: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        test_db: If provided, overrides the DB dependency for testing.
        test_workspace: If provided, sets workspace for testing.
    """
    app = FastAPI(title="Bridle", version="0.2.0")

    if test_workspace is not None:
        from bridle.config import set_workspace
        set_workspace(test_workspace)

    if test_db is not None:
        from bridle.api.deps import set_test_db
        set_test_db(test_db)

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

    app.include_router(tasks_router, prefix="/api/v1")
    app.include_router(plans_router, prefix="/api/v1")
    app.include_router(nodes_router, prefix="/api/v1")
    app.include_router(proposals_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(coding_sessions_router, prefix="/api/v1")
    app.include_router(node_agent_runs_router, prefix="/api/v1")
    app.include_router(plan_change_proposals_router, prefix="/api/v1")

    return app
