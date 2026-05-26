"""AgentGateway — unified service for agent proposal generation.

V1 constraints:
- Agent can only read/write files declared in node.files.
- Agent can only access adjacent interface context via get_accessible_context.
- Agent cannot auto-modify files, run commands, or change node status.
- All proposals are dry-run: persisted but not applied.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.engine.agent_provider import AgentProviderFactory
from bridle.engine.blocker import Blocker
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.proposal import ProposalRecord
from bridle.schemas.proposal import AgentContext, ProposalReadSchema
from bridle.services.node_service import NodeService

logger = logging.getLogger("bridle")


class AgentGateway:
    """Orchestrates agent proposal creation with boundary enforcement."""

    @staticmethod
    async def create_proposal(db: AsyncSession, node_id: str, instruction: str) -> ProposalReadSchema:
        """Generate and persist a dry-run proposal for the given node.

        Raises ValueError subclasses for different failure modes with
        semantic error codes for API mapping.
        """
        # 1. Load active node
        node_record = await AgentGateway._get_active_node(db, node_id)
        if node_record is None:
            raise _NodeNotFoundError("Node not found or not in active plan")

        # 2. Blocker check — don't call provider for blocked nodes
        completed_ids = await AgentGateway._get_completed_ids(db, node_record.plan_id)
        block_result = Blocker.check(node_record, completed_ids)
        if block_result.blocked:
            raise _NodeBlockedError(f"Node blocked: {block_result.reason}")

        # 3. Allowed files (workspace-relative POSIX, normalized for boundary matching)
        allowed_files_raw = list(node_record.files) if node_record.files else []
        allowed_seen: set[str] = set()
        allowed_files: list[str] = []
        for f in allowed_files_raw:
            key = ProposalPathValidator.normalize_workspace_relative(str(f))
            if not key or key in allowed_seen:
                continue
            allowed_seen.add(key)
            allowed_files.append(key)

        # 4. Accessible context
        accessible_context = await NodeService.get_accessible_context(db, node_id)

        # 5. Build AgentContext
        ctx = AgentContext(
            instruction=instruction,
            node={
                "id": node_record.plan_node_id,
                "title": node_record.title,
                "goal": node_record.goal,
                "node_type": node_record.node_type,
                "depends_on": node_record.depends_on,
            },
            allowed_files=allowed_files,
            tests=node_record.tests if isinstance(node_record.tests, list) else [],
            metrics=node_record.metrics if isinstance(node_record.metrics, dict) else {},
            constraints=node_record.constraints if isinstance(node_record.constraints, dict) else {},
            review_checks=node_record.review_checks if isinstance(node_record.review_checks, list) else [],
            expected_outputs=node_record.expected_outputs if isinstance(node_record.expected_outputs, dict) else {},
            accessible_context=accessible_context,
        )

        # 6. Get provider from factory
        provider = AgentProviderFactory.create()
        provider_cfg = AgentProviderFactory.get_config()
        timeout_seconds = float(provider_cfg["timeout_seconds"])

        # 7. Log start
        _log_provider_event("proposal_provider_started", "started",
            node_id=node_id, plan_node_id=node_record.plan_node_id,
            provider=provider.name, model=provider_cfg["model"])

        # 8. Call provider
        start = time.monotonic()
        try:
            proposal = await asyncio.wait_for(
                provider.generate(ctx),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            _log_provider_event("proposal_provider_failed", "failed",
                node_id=node_id, plan_node_id=node_record.plan_node_id,
                provider=provider.name, model=provider_cfg["model"],
                duration_ms=duration_ms,
                error_code="timeout")
            raise _AgentProviderError(
                "Agent provider failed",
                provider=provider.name,
                reason="timeout",
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            _log_provider_event("proposal_provider_failed", "failed",
                node_id=node_id, plan_node_id=node_record.plan_node_id,
                provider=provider.name, model=provider_cfg["model"],
                duration_ms=duration_ms,
                error_code=type(exc).__name__)
            raise _AgentProviderError(
                f"Agent provider failed: {exc}",
                provider=provider.name,
                reason=type(exc).__name__,
            )

        duration_ms = int((time.monotonic() - start) * 1000)

        # 9. Validate proposal output is well-formed
        if not proposal.summary or not proposal.summary.strip():
            _log_provider_event("proposal_provider_failed", "failed",
                node_id=node_id, plan_node_id=node_record.plan_node_id,
                provider=provider.name, model=provider_cfg["model"],
                duration_ms=duration_ms,
                error_code="EmptySummary")
            raise _AgentProviderError(
                "Agent provider returned empty summary",
                provider=provider.name,
                reason="EmptySummary",
            )

        # 10. Path boundary validation
        file_patches_dicts = [fp.model_dump() for fp in proposal.file_patches]
        path_errors = ProposalPathValidator.validate(file_patches_dicts, allowed_files)
        if path_errors:
            offending = ProposalPathValidator.first_offending_patch_path(file_patches_dicts, allowed_files)
            boundary_details: dict = {"errors": path_errors}
            if offending:
                boundary_details["path"] = offending
            _log_provider_event("proposal_boundary_rejected", "rejected",
                node_id=node_id, plan_node_id=node_record.plan_node_id,
                provider=provider.name, model=provider_cfg["model"],
                duration_ms=duration_ms,
                error_code="PathBoundaryError",
                detail_str="; ".join(path_errors))
            raise _ProposalBoundaryError(
                "Proposal violates node file boundary",
                details=boundary_details,
            )

        # 11. Log success
        _log_provider_event("proposal_provider_completed", "completed",
            node_id=node_id, plan_node_id=node_record.plan_node_id,
            provider=provider.name, model=provider_cfg["model"],
            duration_ms=duration_ms)

        # 12. Persist
        record = ProposalRecord(
            node_id=node_id,
            plan_node_id=node_record.plan_node_id,
            instruction=instruction,
            allowed_files=allowed_files,
            accessible_context=accessible_context,
            proposal=proposal.model_dump(),
            status="proposed",
            source="agent",
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

        return ProposalReadSchema.model_validate(record)

    @staticmethod
    async def list_proposals(db: AsyncSession, node_id: str) -> list[ProposalReadSchema]:
        """List all proposals for a given node."""
        node = await AgentGateway._get_active_node(db, node_id)
        if node is None:
            raise ValueError("Node not found or not in active plan")

        result = await db.execute(
            select(ProposalRecord)
            .where(ProposalRecord.node_id == node_id)
            .order_by(ProposalRecord.created_at.desc())
        )
        records = result.scalars().all()
        return [ProposalReadSchema.model_validate(r) for r in records]

    @staticmethod
    async def _get_active_node(db: AsyncSession, node_id: str) -> NodeRecord | None:
        result = await db.execute(
            select(NodeRecord)
            .join(PlanRecord, NodeRecord.plan_id == PlanRecord.id)
            .where(
                NodeRecord.id == node_id,
                PlanRecord.status == "active",
                NodeRecord.status != "archived",
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_completed_ids(db: AsyncSession, plan_id: str) -> set[str]:
        result = await db.execute(select(NodeRecord).where(NodeRecord.plan_id == plan_id))
        nodes = result.scalars().all()
        return {n.plan_node_id for n in nodes if n.status == "completed"}


# ---------------------------------------------------------------------------
# Internal error classes with semantic codes
# ---------------------------------------------------------------------------


class _NodeNotFoundError(ValueError):
    pass


class _NodeBlockedError(ValueError):
    pass


class _AgentProviderError(ValueError):
    def __init__(self, message: str, provider: str, reason: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.reason = reason


class _ProposalBoundaryError(ValueError):
    def __init__(self, message: str, details: dict) -> None:
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------------------
# Provider-level JSONL logging
# ---------------------------------------------------------------------------


def _log_provider_event(
    action: str,
    status: str,
    *,
    node_id: str,
    plan_node_id: str,
    provider: str,
    model: str,
    duration_ms: int | None = None,
    error_code: str | None = None,
    detail_str: str | None = None,
) -> None:
    """Emit a provider-level structured log event.

    Never logs: API key, full prompt, full source, full diff, sensitive field values.
    """
    detail: dict = {
        "provider": provider,
        "model": model,
    }
    if duration_ms is not None:
        detail["duration_ms"] = duration_ms
    if error_code is not None:
        detail["error_code"] = error_code
    if detail_str is not None:
        detail["detail"] = detail_str

    logger.info(
        action,
        extra={
            "action": action,
            "status": status,
            "node_id": node_id,
            "plan_node_id": plan_node_id,
            "detail": detail,
        },
    )
