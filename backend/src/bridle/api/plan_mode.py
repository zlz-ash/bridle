"""Plan Mode API — stateless planner converse."""
from __future__ import annotations

from fastapi import APIRouter

from bridle.api.errors import BadGatewayError, BridleError, GatewayTimeoutError
from bridle.schemas.plan_mode import PlanModeConverseRequest, PlanModeResponseSchema
from bridle.services.plan_mode_service import (
    PlanModeService,
    PlannerAuthError,
    PlannerProviderError,
    PlannerTimeoutError,
)

router = APIRouter(prefix="/plan-mode", tags=["plan-mode"])


@router.post("/converse")
async def plan_mode_converse(data: PlanModeConverseRequest) -> dict:
    try:
        result = await PlanModeService.converse(data.history, data.workspace_overview)
    except PlannerTimeoutError as exc:
        raise GatewayTimeoutError(
            resource="plan_mode",
            message=str(exc),
            error_code="planner_timeout",
        ) from exc
    except PlannerAuthError as exc:
        raise BadGatewayError(
            resource="plan_mode",
            message=str(exc),
            error_code="planner_auth_failed",
        ) from exc
    except PlannerProviderError as exc:
        raise BridleError(
            code="planner_provider_error",
            message=exc.message,
            status_code=500,
            resource="plan_mode",
        ) from exc
    return result.model_dump()
