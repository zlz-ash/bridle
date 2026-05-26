"""Plan change proposal API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.schemas.plan_change import PlanChangeProposalCreateSchema
from bridle.services.plan_change_proposal_service import PlanChangeProposalService

router = APIRouter(prefix="/plan-change-proposals", tags=["plan-change-proposals"])


class RejectBody(BaseModel):
    reason: str = ""


@router.post("")
async def create_plan_change_proposal(
    data: PlanChangeProposalCreateSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await PlanChangeProposalService.create_proposal(db, data)
    return proposal.model_dump()


@router.get("/{proposal_id}")
async def get_plan_change_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    proposal = await PlanChangeProposalService.get_proposal(db, proposal_id)
    return proposal.model_dump()


@router.post("/{proposal_id}/approve")
async def approve_plan_change_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    proposal = await PlanChangeProposalService.approve(db, proposal_id)
    return proposal.model_dump()


@router.post("/{proposal_id}/reject")
async def reject_plan_change_proposal(
    proposal_id: str,
    body: RejectBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await PlanChangeProposalService.reject(db, proposal_id, body.reason)
    return proposal.model_dump()


@router.post("/{proposal_id}/apply")
async def apply_plan_change_proposal(proposal_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    proposal = await PlanChangeProposalService.apply(db, proposal_id)
    return proposal.model_dump()
