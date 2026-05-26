"""Tasks API router."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.api.errors import NotFoundError, ValidationError
from bridle.schemas.task import TaskCreateSchema, TaskReadSchema
from bridle.services.plan_service import PlanService
from bridle.services.task_service import TaskService

router = APIRouter(tags=["tasks"])


@router.post("/tasks", response_model=TaskReadSchema, status_code=201)
async def create_task(data: TaskCreateSchema, db: AsyncSession = Depends(get_db)) -> TaskReadSchema:
    return await TaskService.create(db, data)


@router.get("/tasks", response_model=list[TaskReadSchema])
async def list_tasks(db: AsyncSession = Depends(get_db)) -> list[TaskReadSchema]:
    return await TaskService.list_all(db)


@router.get("/tasks/{task_id}", response_model=TaskReadSchema)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)) -> TaskReadSchema:
    task = await TaskService.get_by_id(db, task_id)
    if task is None:
        raise NotFoundError(resource="task", message="Task not found")
    return task


@router.post("/tasks/{task_id}/plan/import")
async def import_plan(task_id: str, data: dict, db: AsyncSession = Depends(get_db)) -> dict:
    """Import a plan as the workspace's global current plan."""
    from bridle.schemas.plan import PlanImportSchema

    # Verify task exists
    task = await TaskService.get_by_id(db, task_id)
    if task is None:
        raise NotFoundError(resource="task", message="Task not found")
    try:
        plan_schema = PlanImportSchema(**data)
    except Exception as e:
        raise ValidationError(resource="plan", message=str(e))
    try:
        return await PlanService.import_plan(db, task_id, plan_schema)
    except ValueError as e:
        raise ValidationError(resource="plan", message=str(e))


@router.get("/tasks/{task_id}/graph")
async def get_task_graph(task_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Return the node graph for the current active plan under this task."""
    from bridle.services.node_service import NodeService

    return await NodeService.get_graph(db, task_id)
