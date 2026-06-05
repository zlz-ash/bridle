"""NodeAgentRunService — node-level async agent run lifecycle."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.errors import ConflictError, NotFoundError, ValidationError
from bridle.coding_config import (
    ACTIVE_RUN_STATUSES,
    CODING_CONFIG,
    HEARTBEAT_ALLOWED_STATUSES,
    TERMINAL_RUN_STATUSES,
)
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.events.bus import publish_event_safe
from bridle.logging.jsonl import log_event
from bridle.models.node import NodeRecord
from bridle.models.node_agent_heartbeat import NodeAgentHeartbeatRecord
from bridle.models.node_agent_result import NodeAgentResultRecord
from bridle.models.node_agent_result import NodeAgentResultRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.models.node_agent_run_lock import NodeAgentRunLockRecord
from bridle.models.plan import PlanRecord
from bridle.models.proposal import ProposalRecord
from bridle.schemas.coding import HeartbeatSchema, NodeAgentResultSubmitSchema, NodeAgentRunReadSchema
from bridle.services.capability_policy import CapabilityPolicyService
from bridle.utils.datetime_util import utc_now_naive

logger = logging.getLogger("bridle")


class NodeAgentRunService:
    @staticmethod
    async def create_run_with_lock(
        db: AsyncSession,
        *,
        session_id: str,
        node: NodeRecord,
    ) -> NodeAgentRunReadSchema:
        now = utc_now_naive()
        timeout_at = now + timedelta(seconds=CODING_CONFIG.hard_timeout_seconds)
        record = NodeAgentRunRecord(
            session_id=session_id,
            node_id=node.id,
            plan_node_id=node.plan_node_id,
            status="queued",
            phase="initializing",
            attempt=1,
            started_at=now,
            timeout_at=timeout_at,
        )
        db.add(record)
        await db.flush()
        lock = NodeAgentRunLockRecord(
            node_id=node.id,
            run_id=record.id,
            session_id=session_id,
        )
        db.add(lock)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise ConflictError(
                resource="node",
                message="Node already has an active agent run",
                error_code="node_already_running",
                details={"node_id": node.id},
            )
        await db.refresh(record)
        log_event(
            "node_agent_run_created",
            "completed",
            node_id=node.id,
            run_id=record.id,
            detail={"session_id": session_id, "plan_node_id": node.plan_node_id},
        )
        publish_event_safe(
            "node_agent_run_updated",
            {
                "run_id": record.id,
                "node_id": node.id,
                "status": record.status,
                "phase": record.phase,
            },
        )
        return NodeAgentRunService._to_read(record)

    @staticmethod
    async def release_lock(db: AsyncSession, node_id: str) -> None:
        result = await db.execute(
            select(NodeAgentRunLockRecord).where(NodeAgentRunLockRecord.node_id == node_id)
        )
        lock = result.scalar_one_or_none()
        if lock is not None:
            await db.delete(lock)
            await db.flush()

    @staticmethod
    async def get_run(db: AsyncSession, run_id: str) -> NodeAgentRunReadSchema:
        record = await NodeAgentRunService._load_run(db, run_id)
        result_rec = await NodeAgentRunService._latest_result(db, run_id)
        return NodeAgentRunService._to_read(record, result_record=result_rec)

    @staticmethod
    async def record_heartbeat(db: AsyncSession, run_id: str, data: HeartbeatSchema) -> NodeAgentRunReadSchema:
        record = await NodeAgentRunService._load_run(db, run_id)
        if record.status in TERMINAL_RUN_STATUSES:
            raise ConflictError(resource="node_agent_run", message="Run is already terminal")
        if record.status == "queued":
            raise ValidationError(
                resource="heartbeat",
                message="Heartbeat not allowed while run is queued",
            )
        if data.run_id != run_id or data.node_id != record.node_id:
            raise ValidationError(
                resource="heartbeat",
                message="run_id/node_id mismatch",
                details={"run_id": run_id, "node_id": data.node_id},
            )
        if data.status not in HEARTBEAT_ALLOWED_STATUSES:
            raise ValidationError(
                resource="heartbeat",
                message="Heartbeat status not allowed",
                details={"status": data.status},
            )

        msg = data.message[:CODING_CONFIG.heartbeat_message_max_len]
        record.last_heartbeat_at = utc_now_naive()
        record.status = data.status
        if data.phase:
            record.phase = data.phase
        if data.blocked_reason:
            record.blocked_reason = data.blocked_reason[:500]

        hb = NodeAgentHeartbeatRecord(
            run_id=run_id,
            node_id=record.node_id,
            status=data.status,
            phase=data.phase,
            message=msg,
            progress=data.progress,
        )
        db.add(hb)
        await db.commit()
        await db.refresh(record)

        log_event(
            "node_agent_heartbeat",
            "completed",
            node_id=record.node_id,
            run_id=run_id,
            detail={"phase": data.phase, "progress": data.progress},
        )
        publish_event_safe(
            "node_agent_run_updated",
            {
                "run_id": run_id,
                "node_id": record.node_id,
                "status": record.status,
                "phase": record.phase,
            },
        )
        return NodeAgentRunService._to_read(record)

    @staticmethod
    async def submit_result(
        db: AsyncSession,
        run_id: str,
        data: NodeAgentResultSubmitSchema,
    ) -> NodeAgentRunReadSchema:
        record = await NodeAgentRunService._load_run(db, run_id)
        if record.status in TERMINAL_RUN_STATUSES:
            raise ConflictError(resource="node_agent_run", message="Run is already terminal")
        if data.run_id != run_id or data.node_id != record.node_id:
            raise ValidationError(resource="result", message="run_id/node_id mismatch")

        if data.result_type not in CapabilityPolicyService.allowed_result_types():
            raise ValidationError(
                resource="result",
                message="result_type not allowed",
                details={"result_type": data.result_type},
            )

        if data.result_type == "proposal" and data.proposal_id:
            await NodeAgentRunService._validate_proposal_boundary(db, data.proposal_id, record.node_id)

        now = utc_now_naive()
        result_rec = NodeAgentResultRecord(
            run_id=run_id,
            node_id=record.node_id,
            status=data.status,
            result_type=data.result_type,
            proposal_id=data.proposal_id,
            summary=data.summary[:2000],
            confidence=data.confidence,
            issues=data.issues,
            recommended_next_action=data.recommended_next_action,
            payload=data.model_dump(),
        )
        db.add(result_rec)

        if data.result_type == "blocked_report":
            record.status = "blocked"
            record.blocked_reason = (data.summary or "blocked_report")[:500]
            await NodeAgentRunService._set_node_needs_review(db, record.node_id)
            log_event("node_agent_run_blocked", "completed", node_id=record.node_id, run_id=run_id)
        elif data.status == "completed":
            record.status = "completed"
            record.phase = "finalizing"
            record.finished_at = now
            if record.started_at:
                record.duration_ms = int((now - record.started_at).total_seconds() * 1000)
            record.result_summary = data.summary[:500]
            await NodeAgentRunService._set_node_needs_review(db, record.node_id)
        else:
            # 非 completed 的非 blocked 终态（如 failed/timed_out 经 submit_result 路径）
            # 仍要同步 NodeRecord 状态，UI 才能看到
            if (data.status if data.status in TERMINAL_RUN_STATUSES else "failed") in {"failed", "timed_out"}:
                await NodeAgentRunService._set_node_failed_retryable(db, record.node_id)
            record.status = data.status if data.status in TERMINAL_RUN_STATUSES else "failed"
            record.finished_at = now

        if record.status in TERMINAL_RUN_STATUSES:
            await NodeAgentRunService.release_lock(db, record.node_id)
        await db.commit()
        await db.refresh(record)
        log_event(
            "node_agent_result_submitted",
            "completed",
            node_id=record.node_id,
            run_id=run_id,
            detail={"result_type": data.result_type},
        )
        publish_event_safe(
            "node_agent_run_updated",
            {
                "run_id": run_id,
                "node_id": record.node_id,
                "status": record.status,
                "phase": record.phase,
            },
        )
        return NodeAgentRunService._to_read(record)

    @staticmethod
    async def cancel_run(db: AsyncSession, run_id: str) -> NodeAgentRunReadSchema:
        record = await NodeAgentRunService._load_run(db, run_id)
        if record.status in TERMINAL_RUN_STATUSES:
            raise ConflictError(resource="node_agent_run", message="Run is already terminal")
        record.status = "cancelled"
        record.finished_at = utc_now_naive()
        await NodeAgentRunService.release_lock(db, record.node_id)
        await db.commit()
        await db.refresh(record)
        log_event("node_agent_run_cancelled", "completed", node_id=record.node_id, run_id=run_id)
        publish_event_safe(
            "node_agent_run_updated",
            {
                "run_id": run_id,
                "node_id": record.node_id,
                "status": record.status,
                "phase": record.phase,
            },
        )
        return NodeAgentRunService._to_read(record)

    @staticmethod
    async def get_active_node(db: AsyncSession, node_id: str) -> NodeRecord | None:
        result = await db.execute(
            select(NodeRecord)
            .join(PlanRecord, NodeRecord.plan_id == PlanRecord.id)
            .where(
                NodeRecord.id == node_id,
                PlanRecord.status == "active",
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _validate_proposal_boundary(db: AsyncSession, proposal_id: str, node_id: str) -> None:
        prop_result = await db.execute(
            select(ProposalRecord).where(ProposalRecord.id == proposal_id, ProposalRecord.node_id == node_id)
        )
        proposal = prop_result.scalar_one_or_none()
        if proposal is None:
            raise NotFoundError(resource="proposal", message="Proposal not found")
        node_result = await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        node = node_result.scalar_one()
        allowed = [
            ProposalPathValidator.normalize_workspace_relative(str(f))
            for f in (node.files or [])
        ]
        patches = proposal.proposal.get("file_patches", []) if isinstance(proposal.proposal, dict) else []
        errors = ProposalPathValidator.validate(patches, allowed)
        if errors:
            raise ConflictError(
                resource="proposal",
                message="Proposal violates node file boundary",
                error_code="proposal_boundary_error",
                details={"errors": errors},
            )

    @staticmethod
    async def _set_node_needs_review(db: AsyncSession, node_id: str) -> None:
        await NodeAgentRunService._set_node_status(db, node_id, "needs_review")

    @staticmethod
    async def _set_node_failed_retryable(db: AsyncSession, node_id: str) -> None:
        await NodeAgentRunService._set_node_status(db, node_id, "failed_retryable")

    @staticmethod
    async def _set_node_status(db: AsyncSession, node_id: str, new_status: str) -> None:
        result = await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        node = result.scalar_one_or_none()
        if node is None:
            return
        old_status = node.status
        node.status = new_status
        if old_status != new_status:
            publish_event_safe(
                "node_status_changed",
                {
                    "node_id": node.id,
                    "plan_node_id": node.plan_node_id,
                    "old_status": old_status,
                    "new_status": new_status,
                },
            )

    @staticmethod
    async def _load_run(db: AsyncSession, run_id: str) -> NodeAgentRunRecord:
        result = await db.execute(
            select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundError(resource="node_agent_run", message="Node agent run not found")
        return record

    @staticmethod
    def _to_read(
        record: NodeAgentRunRecord,
        *,
        result_record: NodeAgentResultRecord | None = None,
    ) -> NodeAgentRunReadSchema:
        payload = result_record.payload if result_record and isinstance(result_record.payload, dict) else {}
        return NodeAgentRunReadSchema(
            run_id=record.id,
            session_id=record.session_id,
            node_id=record.node_id,
            plan_node_id=record.plan_node_id,
            status=record.status,
            phase=record.phase,
            attempt=record.attempt,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            last_heartbeat_at=record.last_heartbeat_at,
            timeout_at=record.timeout_at,
            duration_ms=record.duration_ms,
            blocked_reason=record.blocked_reason,
            result_summary=record.result_summary,
            container_id=record.container_id,
            container_status=record.container_status,
            container_health=record.container_health,
            container_error=record.container_error,
            container_logs_summary=record.container_logs_summary,
            diagnostic_path=record.diagnostic_path,
            error_code=record.blocked_reason if record.status in {"failed", "timed_out", "blocked"} else None,
            test_summary=payload.get("test_summary"),
            metrics_summary=payload.get("metrics_summary"),
            integration_result=payload.get("integration"),
            budget_report=payload.get("budget_report")
            if isinstance(payload.get("budget_report"), dict)
            else None,
            replan_decision=payload.get("replan_decision")
            if isinstance(payload.get("replan_decision"), dict)
            else None,
        )

    @staticmethod
    async def _latest_result(db: AsyncSession, run_id: str) -> NodeAgentResultRecord | None:
        result = await db.execute(
            select(NodeAgentResultRecord)
            .where(NodeAgentResultRecord.run_id == run_id)
            .order_by(NodeAgentResultRecord.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_latest_for_node(db: AsyncSession, node_id: str) -> NodeAgentRunReadSchema | None:
        runs = await NodeAgentRunService.list_by_node(db, node_id)
        return runs[0] if runs else None

    @staticmethod
    async def list_by_node(db: AsyncSession, node_id: str) -> list[NodeAgentRunReadSchema]:
        result = await db.execute(
            select(NodeAgentRunRecord)
            .where(NodeAgentRunRecord.node_id == node_id)
            .order_by(
                NodeAgentRunRecord.created_at.desc(),
                NodeAgentRunRecord.started_at.desc(),
            )
        )
        reads: list[NodeAgentRunReadSchema] = []
        for record in result.scalars().all():
            result_rec = await NodeAgentRunService._latest_result(db, record.id)
            reads.append(NodeAgentRunService._to_read(record, result_record=result_rec))
        return reads
