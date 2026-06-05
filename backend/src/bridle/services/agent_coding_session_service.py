"""AgentCodingSessionService — coding mode session lifecycle."""
from __future__ import annotations

import asyncio
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
    CodingSessionListResponseSchema,
    CodingSessionReadSchema,
    EligibleNodeSchema,
    EligibleNodesResponseSchema,
    NodeAgentRunReadSchema,
    SelectNodeIntentSchema,
)
from bridle.services.capability_policy import CapabilityPolicyService
from bridle.services.node_agent_run_service import NodeAgentRunService
from bridle.services.node_agent_worker import NodeAgentWorkerService
from bridle.services.node_eligibility import MAX_NODE_SELECT_ATTEMPTS, NodeEligibilityService
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
        skip_container = os.getenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "").strip() == "1"
        # Tests inject db via set_test_db; the async finalize path needs a real
        # async_session factory it doesn't have, so keep the old synchronous behavior
        # for tests. Production (no test_db) returns 'creating' immediately and lets
        # the frontend poll for 'active'.
        synchronous = skip_container or is_test_mode()
        initial_status = "active" if synchronous else "creating"
        record = AgentCodingSessionRecord(
            plan_id=plan_id,
            status=initial_status,
            mode="coding",
            auto_continue_budget=budget,
            auto_continue_used=0,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

        log_event(
            "coding_session_started",
            "completed",
            detail={"session_id": record.id, "plan_id": plan_id, "status": initial_status},
        )
        if skip_container:
            return AgentCodingSessionService._to_read(record, main_agent_container=None)
        if is_test_mode():
            # Synchronous container path used by the existing test suite.
            try:
                metadata = MainAgentContainerService(get_config().workspace).record_for_session(
                    session_id=record.id, plan_id=plan_id,
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
                log_event(
                    "coding_session_failed",
                    "failed",
                    detail={"session_id": record.id, "reason": str(exc)},
                )
                return AgentCodingSessionService._to_read(record, main_agent_container=None)

        # Production: container startup is slow (3-15s on Windows Docker Desktop).
        # Schedule it as a background task so the HTTP response returns within ms; the
        # frontend polls the session status until 'active' or 'failed'.
        workspace = get_config().workspace
        asyncio.create_task(
            AgentCodingSessionService._finalize_container_async(
                session_id=record.id, plan_id=plan_id, workspace=workspace,
            )
        )
        return AgentCodingSessionService._to_read(record, main_agent_container=None)

    @staticmethod
    async def _finalize_container_async(*, session_id: str, plan_id: str, workspace) -> None:
        """Run the synchronous ``docker run`` in an executor; flip status on completion."""
        from bridle.database import async_session

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: MainAgentContainerService(workspace).record_for_session(
                    session_id=session_id, plan_id=plan_id,
                ),
            )
            async with async_session() as db:
                record = (
                    await db.execute(
                        select(AgentCodingSessionRecord).where(AgentCodingSessionRecord.id == session_id)
                    )
                ).scalar_one_or_none()
                if record is not None and record.status == "creating":
                    record.status = "active"
                    await db.commit()
            log_event(
                "coding_session_container_ready",
                "completed",
                detail={"session_id": session_id, "plan_id": plan_id},
            )
        except Exception as exc:
            logger.warning(
                "main_agent_container_startup_failed",
                extra={
                    "action": "main_agent_container_startup_failed",
                    "status": "failed",
                    "detail": {"session_id": session_id, "error": str(exc)},
                },
            )
            async with async_session() as db:
                record = (
                    await db.execute(
                        select(AgentCodingSessionRecord).where(AgentCodingSessionRecord.id == session_id)
                    )
                ).scalar_one_or_none()
                if record is not None and record.status == "creating":
                    record.status = "failed"
                    await db.commit()
            log_event(
                "coding_session_failed",
                "failed",
                detail={"session_id": session_id, "reason": str(exc)},
            )

    @staticmethod
    async def list_sessions(
        db: AsyncSession,
        *,
        status: str = "all",
        plan_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CodingSessionReadSchema], int]:
        from bridle.api.errors import ValidationError
        from sqlalchemy import func

        allowed_status = {"active", "cancelled", "completed", "creating", "failed", "all"}
        if status not in allowed_status:
            raise ValidationError(
                resource="coding_session",
                message="Invalid status filter",
                details={"status": status, "allowed": sorted(allowed_status)},
            )
        if limit < 1 or limit > 200:
            raise ValidationError(
                resource="coding_session",
                message="limit must be between 1 and 200",
                details={"limit": limit},
            )
        if offset < 0:
            raise ValidationError(
                resource="coding_session",
                message="offset must be non-negative",
                details={"offset": offset},
            )

        filters = []
        if status != "all":
            filters.append(AgentCodingSessionRecord.status == status)
        if plan_id:
            filters.append(AgentCodingSessionRecord.plan_id == plan_id)

        count_stmt = select(func.count()).select_from(AgentCodingSessionRecord)
        if filters:
            count_stmt = count_stmt.where(*filters)
        total = int((await db.execute(count_stmt)).scalar_one())

        stmt = (
            select(AgentCodingSessionRecord)
            .order_by(AgentCodingSessionRecord.created_at.desc(), AgentCodingSessionRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if filters:
            stmt = stmt.where(*filters)
        rows = (await db.execute(stmt)).scalars().all()
        sessions = [AgentCodingSessionService._to_read(row, main_agent_container=None) for row in rows]
        return sessions, total

    @staticmethod
    async def get_session(db: AsyncSession, session_id: str) -> CodingSessionReadSchema:
        record = await AgentCodingSessionService._load_session(db, session_id)
        return AgentCodingSessionService._to_read(record)

    @staticmethod
    async def fail_session(db: AsyncSession, session_id: str, *, reason: str) -> CodingSessionReadSchema:
        record = await AgentCodingSessionService._load_session(db, session_id)
        if record.status in ("failed", "completed", "cancelled"):
            return AgentCodingSessionService._to_read(record)
        record.status = "failed"
        await db.commit()
        await db.refresh(record)
        log_event(
            "coding_session_failed",
            "completed",
            detail={"session_id": session_id, "reason": reason},
        )
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
    async def get_recent_failed_runs(
        db: AsyncSession,
        session_id: str,
        *,
        limit: int = 3,
    ) -> list[dict]:
        from bridle.models.node import NodeRecord
        from bridle.models.node_agent_result import NodeAgentResultRecord
        from bridle.models.node_agent_run import NodeAgentRunRecord

        await AgentCodingSessionService._load_session(db, session_id)
        cap = max(1, min(int(limit), 10))
        result = await db.execute(
            select(NodeAgentRunRecord, NodeRecord, NodeAgentResultRecord)
            .join(NodeRecord, NodeAgentRunRecord.node_id == NodeRecord.id)
            .outerjoin(
                NodeAgentResultRecord,
                NodeAgentResultRecord.run_id == NodeAgentRunRecord.id,
            )
            .where(
                NodeAgentRunRecord.session_id == session_id,
                NodeAgentRunRecord.status.in_(("failed", "timed_out")),
                NodeAgentRunRecord.finished_at.isnot(None),
            )
            .order_by(NodeAgentRunRecord.finished_at.desc())
            .limit(cap)
        )
        rows: list[dict] = []
        for run, node, agent_result in result.all():
            finished = run.finished_at.isoformat() if run.finished_at else None
            rows.append({
                "run_id": run.id,
                "node_id": run.node_id,
                "plan_node_id": run.plan_node_id,
                "title": node.title,
                "status": run.status,
                "blocked_reason": run.blocked_reason,
                "result_summary": run.result_summary or (agent_result.summary if agent_result else ""),
                "result_type": agent_result.result_type if agent_result else None,
                "recommended_next_action": (
                    agent_result.recommended_next_action if agent_result else None
                ),
                "finished_at": finished,
            })
        return rows

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
            if reason == "node_attempts_exhausted":
                error_code = "node_attempts_exhausted"
            elif reason == "node_already_running":
                error_code = "node_already_running"
            else:
                error_code = "node_not_eligible"
            raise ConflictError(
                resource="node",
                message="Node is not eligible for agent run",
                error_code=error_code,
                details={"node_id": intent.node_id, "reason": reason, "blocked_by": blocked_by},
            )

        attempt_used = await NodeEligibilityService.count_prior_runs(db, node.id)
        if attempt_used >= MAX_NODE_SELECT_ATTEMPTS:
            log_event(
                "model_node_selection_rejected",
                "rejected",
                detail={
                    "session_id": session_id,
                    "node_id": intent.node_id,
                    "reason": "node_attempts_exhausted",
                    "attempts_used": attempt_used,
                },
            )
            raise ConflictError(
                resource="node",
                message="Node has exhausted retry attempts",
                error_code="node_attempts_exhausted",
                details={"node_id": intent.node_id, "attempts_used": attempt_used},
            )

        run = await NodeAgentRunService.create_run_with_lock(
            db, session_id=session_id, node=node,
        )
        from bridle.services.master_skill_assignment import assign_skill_for_node, persist_assignment

        assignment = assign_skill_for_node(node)
        persist_assignment(run.run_id, assignment)
        record.auto_continue_used += 1
        await db.commit()

        log_event(
            "model_node_selected",
            "completed",
            node_id=node.id,
            run_id=run.run_id,
            detail={
                "session_id": session_id,
                "plan_node_id": node.plan_node_id,
                "skill_assignment": {
                    "use_skill": assignment.get("use_skill"),
                    "skill_id": assignment.get("skill_id"),
                    "submodule": assignment.get("submodule"),
                    "assigned_by": assignment.get("assigned_by"),
                },
            },
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
