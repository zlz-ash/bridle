"""Agent proposals API router — dry-run proposal generation and listing."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.api.errors import ConflictError, NotFoundError, ValidationError
from bridle.schemas.proposal import ProposalCreateSchema
from bridle.services.agent_gateway import (
    AgentGateway,
    _AgentProviderError,
    _NodeBlockedError,
    _NodeNotFoundError,
    _ProposalBoundaryError,
)

router = APIRouter(tags=["proposals"])


@router.post("/nodes/{node_id}/agent/proposals")
async def create_proposal(node_id: str, data: ProposalCreateSchema, db: AsyncSession = Depends(get_db)) -> dict:
    """Generate a dry-run agent proposal for a node.

    The proposal is a structured plan (summary, file_patches, tests_to_run)
    bound by node.files and adjacent interface context. The proposal is
    persisted but NOT applied — no files are modified.
    """
    try:
        proposal = await AgentGateway.create_proposal(db, node_id, data.instruction)
    except _NodeNotFoundError as e:
        raise NotFoundError(resource="node", message=str(e))
    except _NodeBlockedError as e:
        raise ConflictError(resource="node", message=str(e))
    except _ProposalBoundaryError as e:
        raise ConflictError(
            resource="proposal",
            message="Proposal violates node file boundary",
            details=e.details if hasattr(e, "details") else None,
            error_code="proposal_boundary_error",
        )
    except _AgentProviderError as e:
        raise ConflictError(
            resource="proposal",
            message="Agent provider failed",
            details={
                "provider": e.provider if hasattr(e, "provider") else "unknown",
                "reason": e.reason if hasattr(e, "reason") else "unknown",
            },
            error_code="agent_provider_error",
        )
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower() or "not in active plan" in msg.lower():
            raise NotFoundError(resource="node", message=msg)
        if "blocked" in msg.lower():
            raise ConflictError(resource="node", message=msg)
        raise ValidationError(resource="proposal", message=msg)
    return proposal.model_dump()


@router.get("/nodes/{node_id}/agent/proposals")
async def list_proposals(node_id: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """List all proposals for a node, newest first."""
    try:
        proposals = await AgentGateway.list_proposals(db, node_id)
    except ValueError as e:
        raise NotFoundError(resource="node", message=str(e))
    return [p.model_dump() for p in proposals]
