"""Node agent run API — heartbeat, result, cancel."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.schemas.coding import HeartbeatSchema, NodeAgentResultSubmitSchema
from bridle.services.node_agent_run_service import NodeAgentRunService

router = APIRouter(prefix="/node-agent-runs", tags=["node-agent-runs"])


@router.get("/{run_id}")
async def get_node_agent_run(run_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    run = await NodeAgentRunService.get_run(db, run_id)
    return run.model_dump()


@router.post("/{run_id}/heartbeat")
async def post_heartbeat(
    run_id: str,
    data: HeartbeatSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await NodeAgentRunService.record_heartbeat(db, run_id, data)
    return run.model_dump()


@router.post("/{run_id}/result")
async def post_result(
    run_id: str,
    data: NodeAgentResultSubmitSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await NodeAgentRunService.submit_result(db, run_id, data)
    return run.model_dump()


@router.post("/{run_id}/cancel")
async def cancel_node_agent_run(run_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    run = await NodeAgentRunService.cancel_run(db, run_id)
    return run.model_dump()
