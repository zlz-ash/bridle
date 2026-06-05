"""Plans API router — current plan operations."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.api.errors import NotFoundError, PlanNotExecutableError, ValidationError
from bridle.services.complexity_negotiation_service import (
    PlanComplexityFailedError,
    ReplanRequestedError,
)
from bridle.schemas.plan import PlanImportSchema, PlanPatchSchema
from bridle.services.node_service import NodeService
from bridle.services.plan_service import PlanService

router = APIRouter(tags=["plans"])


@router.get("/plan/current")
async def get_current_plan(db: AsyncSession = Depends(get_db)) -> dict:
    """Return the workspace's single active plan with its nodes, or 404.

    Also resyncs current-plan.json if it's out of date with the DB.
    """
    plan = await PlanService.get_current_with_resync(db)
    if plan is None:
        raise NotFoundError(resource="plan", message="No active plan")
    nodes = await NodeService.list_by_task(db, plan.task_id)
    return {**plan.model_dump(), "nodes": [n.model_dump() for n in nodes]}


@router.put("/plan/current")
async def replace_current_plan(data: PlanImportSchema, db: AsyncSession = Depends(get_db)) -> dict:
    """Full replacement of the current plan.

    Archives the old plan, creates a new one from the provided data,
    writes plan-summary.json for the old plan, and updates current-plan.json.
    """
    plan = await PlanService.get_current(db)
    if plan is None:
        raise NotFoundError(resource="plan", message="No active plan to replace")
    try:
        return await PlanService.replace_plan(db, plan.task_id, data)
    except ReplanRequestedError as exc:
        raise PlanNotExecutableError(
            last_issues=[],
            rounds_used=0,
            failure_reason=f"replan_requested:{exc.reason}",
        ) from exc
    except PlanComplexityFailedError as exc:
        raise PlanNotExecutableError(
            last_issues=[item.to_dict() for item in exc.last_validations],
            rounds_used=exc.rounds_used,
            failure_reason=exc.failure_reason,
        ) from exc
    except ValueError as e:
        raise ValidationError(resource="plan", message=str(e))


@router.patch("/plan/current")
async def patch_current_plan(data: PlanPatchSchema, db: AsyncSession = Depends(get_db)) -> dict:
    """Partial update of the current plan.

    Supports: updating node content, adding nodes, removing nodes,
    and replacing dependencies. Does NOT archive the plan.
    Rejects changes that would create circular dependencies.
    """
    plan = await PlanService.get_current(db)
    if plan is None:
        raise NotFoundError(resource="plan", message="No active plan to patch")
    try:
        return await PlanService.patch_current(db, data)
    except ValueError as e:
        raise ValidationError(resource="plan", message=str(e))


@router.post("/plans/{plan_id}/negotiate-complexity")
async def negotiate_plan_complexity(plan_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    from bridle.models.plan import PlanRecord
    from sqlalchemy import select

    result = await db.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise NotFoundError(resource="plan", message="Plan not found")
    try:
        return await PlanService.renegotiate_complexity(db, plan_id)
    except ReplanRequestedError as exc:
        raise PlanNotExecutableError(
            last_issues=[],
            rounds_used=0,
            failure_reason=f"replan_requested:{exc.reason}",
        ) from exc
    except PlanComplexityFailedError as exc:
        raise PlanNotExecutableError(
            last_issues=[item.to_dict() for item in exc.last_validations],
            rounds_used=exc.rounds_used,
            failure_reason=exc.failure_reason,
        ) from exc
    except ValueError as e:
        raise ValidationError(resource="plan", message=str(e))


@router.get("/plan/current/summary")
async def get_plan_summary(db: AsyncSession = Depends(get_db)) -> dict:
    """Return the plan-summary.json if it exists, or 404."""
    summary = await PlanService.get_summary(db)
    if summary is None:
        raise NotFoundError(resource="plan_summary", message="No plan summary available")
    return summary
