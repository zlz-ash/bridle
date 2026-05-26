"""AgentCodingSessionService — coding mode session lifecycle."""
from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import is_test_mode
from bridle.api.errors import ConflictError, NotFoundError
from bridle.coding_config import CODING_CONFIG
from bridle.config import get_config
from bridle.logging.jsonl import log_event
from bridle.models.agent_coding_session import AgentCodingSessionRecord
from bridle.models.plan import PlanRecord
from bridle.schemas.coding import (
    BlockedNodeSchema,
    CodingSessionReadSchema,
    EligibleNodeSchema,
    EligibleNodesResponseSchema,
    NodeAgentRunReadSchema,
    SelectNodeIntentSchema,
)
from bridle.services.capability_policy import CapabilityPolicyService
from bridle.services.node_agent_run_service import NodeAgentRunService
from bridle.services.node_agent_worker import NodeAgentWorkerService
from bridle.services.node_eligibility import NodeEligibilityService
from bridle.services.main_agent_container_service import MainAgentContainerService

logger = logging.getLogger("bridle")


class AgentCodingSessionService:
    @staticmethod
    async def create_session(
        db: AsyncSession,
        plan_id: str,
        auto_continue_budget: int | None = None,
    ) -> CodingSessionReadSchema:
        result = await db.execute(
            select(PlanRecord).where(PlanRecord.id == plan_id, PlanRecord.status == "active")
        )
        plan = result.scalar_one_or_none()
        if plan is None:
            raise NotFoundError(resource="plan", message="Active plan not found")

        budget = auto_continue_budget if auto_continue_budget is not None else CODING_CONFIG.default_auto_continue_budget
        record = AgentCodingSessionRecord(
            plan_id=plan_id,
            status="active",
            mode="coding",
            auto_continue_budget=budget,
            auto_continue_used=0,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

        log_event("coding_session_started", "completed", detail={"session_id": record.id, "plan_id": plan_id})
        if os.getenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "").strip() == "1":
            return AgentCodingSessionService._to_read(record, main_agent_container=None)
        try:
            metadata = MainAgentContainerService(get_config().workspace).record_for_session(
                session_id=record.id,
                plan_id=plan_id,
            )
            return AgentCodingSessionService._to_read(record, main_agent_container=metadata)
        except (ValueError, RuntimeError) as exc:
            logger.warning(
                "main_agent_container_startup_failed",
                extra={
                    "action": "main_agent_container_startup_failed",
                    "status": "failed",
                    "detail": {"session_id": record.id, "error": str(exc)},
                },
            )
            record.status = "failed"
            await db.commit()
            await db.refresh(record)
            log_event("coding_session_failed", "failed", detail={"session_id": record.id, "reason": str(exc)})
            return AgentCodingSessionService._to_read(record, main_agent_container=None)

    @staticmethod
    async def get_session(db: AsyncSession, session_id: str) -> CodingSessionReadSchema:
        record = await AgentCodingSessionService._load_session(db, session_id)
        return AgentCodingSessionService._to_read(record)

    @staticmethod
    async def cancel_session(db: AsyncSession, session_id: str) -> CodingSessionReadSchema:
        record = await AgentCodingSessionService._load_session(db, session_id)
        if record.status != "active":
            raise ConflictError(resource="coding_session", message="Session is not active")
        record.status = "cancelled"
        await db.commit()
        await db.refresh(record)
        log_event("coding_session_cancelled", "completed", detail={"session_id": session_id})
        return AgentCodingSessionService._to_read(record)

    @staticmethod
    async def get_eligible_nodes(db: AsyncSession, session_id: str) -> EligibleNodesResponseSchema:
        record = await AgentCodingSessionService._load_session(db, session_id)
        if record.status != "active":
            raise ConflictError(
                resource="coding_session",
                message="Coding session is not active",
                error_code="coding_session_not_active",
            )
        eligible, blocked = await NodeEligibilityService.compute(
            db, record.plan_id, session_id=session_id,
        )
        return EligibleNodesResponseSchema(
            session_id=session_id,
            eligible_nodes=[EligibleNodeSchema(**e.__dict__) for e in eligible],
            blocked_nodes=[BlockedNodeSchema(**b.__dict__) for b in blocked],
        )

    @staticmethod
    async def select_node(
        db: AsyncSession,
        session_id: str,
        intent: SelectNodeIntentSchema,
    ) -> NodeAgentRunReadSchema:
        record = await AgentCodingSessionService._load_session(db, session_id)
        if record.status != "active":
            raise ConflictError(
                resource="coding_session",
                message="Coding session is not active",
                error_code="coding_session_not_active",
            )
        if intent.intent != "select_node":
            raise ConflictError(
                resource="coding_session",
                message="Capability denied",
                error_code="capability_denied",
            )
        if record.auto_continue_used >= record.auto_continue_budget:
            log_event(
                "model_node_selection_rejected",
                "rejected",
                detail={"session_id": session_id, "reason": "session_budget_exceeded"},
            )
            raise ConflictError(
                resource="coding_session",
                message="Session auto-continue budget exceeded",
                error_code="session_budget_exceeded",
            )

        node = await NodeAgentRunService.get_active_node(db, intent.node_id)
        if node is None:
            raise NotFoundError(resource="node", message="Node not found", details={"node_id": intent.node_id})
        if node.plan_id != record.plan_id:
            raise ConflictError(
                resource="node",
                message="Node is not in active plan",
                error_code="node_not_in_active_plan",
                details={"node_id": intent.node_id},
            )
        if node.status == "archived":
            raise ConflictError(
                resource="node",
                message="Node is archived",
                error_code="node_archived",
                details={"node_id": intent.node_id},
            )

        ok, reason, blocked_by = await NodeEligibilityService.is_node_eligible(
            db, record.plan_id, intent.node_id,
        )
        if not ok:
            log_event(
                "model_node_selection_rejected",
                "rejected",
                detail={
                    "session_id": session_id,
                    "node_id": intent.node_id,
                    "reason": reason,
                    "blocked_by": blocked_by,
                },
            )
            error_code = "node_already_running" if reason == "node_already_running" else "node_not_eligible"
            raise ConflictError(
                resource="node",
                message="Node is not eligible for agent run",
                error_code=error_code,
                details={"node_id": intent.node_id, "reason": reason, "blocked_by": blocked_by},
            )

        run = await NodeAgentRunService.create_run_with_lock(
            db, session_id=session_id, node=node,
        )
        record.auto_continue_used += 1
        await db.commit()

        log_event(
            "model_node_selected",
            "completed",
            node_id=node.id,
            run_id=run.run_id,
            detail={"session_id": session_id, "plan_node_id": node.plan_node_id},
        )
        if not is_test_mode():
            NodeAgentWorkerService.start(run.run_id)
        return run

    @staticmethod
    async def _load_session(db: AsyncSession, session_id: str) -> AgentCodingSessionRecord:
        result = await db.execute(
            select(AgentCodingSessionRecord).where(AgentCodingSessionRecord.id == session_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundError(resource="coding_session", message="Coding session not found")
        return record

    @staticmethod
    def _to_read(record: AgentCodingSessionRecord, *, main_agent_container: dict | None = None) -> CodingSessionReadSchema:
        if main_agent_container is None:
            try:
                main_agent_container = MainAgentContainerService(
                    get_config().workspace
                ).read_for_session(record.id)
            except RuntimeError:
                main_agent_container = None
        return CodingSessionReadSchema(
            session_id=record.id,
            plan_id=record.plan_id,
            status=record.status,
            mode=record.mode,
            auto_continue_budget=record.auto_continue_budget,
            auto_continue_used=record.auto_continue_used,
            created_at=record.created_at,
            capabilities=CapabilityPolicyService.session_capabilities(),
            main_agent_container=main_agent_container,
        )
