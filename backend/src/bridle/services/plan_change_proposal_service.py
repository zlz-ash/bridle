"""PlanChangeProposalService — human-reviewed plan mutations."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.errors import ConflictError, NotFoundError
from bridle.engine.plan_change_validator import PlanChangeValidator
from bridle.events.bus import publish_event_safe
from bridle.logging.jsonl import log_event
from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.plan_change_proposal import PlanChangeProposalRecord
from bridle.schemas.plan_change import PlanChangeProposalCreateSchema, PlanChangeProposalReadSchema
from bridle.services.plan_service import PlanService

logger = logging.getLogger("bridle")


class PlanChangeProposalService:
    @staticmethod
    async def create_proposal(
        db: AsyncSession,
        data: PlanChangeProposalCreateSchema,
    ) -> PlanChangeProposalReadSchema:
        plan = await PlanChangeProposalService._get_active_plan(db, data.plan_id)
        if plan is None:
            raise NotFoundError(resource="plan", message="Active plan not found")

        val_errors = PlanChangeValidator.validate_change_set(
            [op.model_dump() for op in data.change_set]
        )
        if val_errors:
            raise ConflictError(
                resource="plan_change_proposal",
                message="Invalid change set",
                details={"errors": val_errors},
            )

        record = PlanChangeProposalRecord(
            plan_id=data.plan_id,
            proposal_type=data.proposal_type,
            change_set=[op.model_dump() for op in data.change_set],
            risk_level=data.risk_level,
            requires_human_review=True,
            status="proposed",
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        log_event("plan_change_proposed", "completed", detail={"proposal_id": record.id, "plan_id": data.plan_id})
        publish_event_safe("plan_change_created", {"proposal_id": record.id, "plan_id": data.plan_id})
        return PlanChangeProposalService._to_read(record)

    @staticmethod
    async def get_proposal(db: AsyncSession, proposal_id: str) -> PlanChangeProposalReadSchema:
        record = await PlanChangeProposalService._load(db, proposal_id)
        return PlanChangeProposalService._to_read(record)

    @staticmethod
    async def approve(db: AsyncSession, proposal_id: str) -> PlanChangeProposalReadSchema:
        record = await PlanChangeProposalService._load(db, proposal_id)
        if record.status != "proposed":
            raise ConflictError(resource="plan_change_proposal", message="Proposal is not in proposed status")
        record.status = "approved"
        await db.commit()
        await db.refresh(record)
        log_event("plan_change_approved", "completed", detail={"proposal_id": proposal_id})
        publish_event_safe("plan_change_approved", {"proposal_id": proposal_id})
        return PlanChangeProposalService._to_read(record)

    @staticmethod
    async def reject(db: AsyncSession, proposal_id: str, reason: str = "") -> PlanChangeProposalReadSchema:
        record = await PlanChangeProposalService._load(db, proposal_id)
        if record.status != "proposed":
            raise ConflictError(resource="plan_change_proposal", message="Proposal is not in proposed status")
        record.status = "rejected"
        record.rejection_reason = reason[:2000]
        await db.commit()
        await db.refresh(record)
        log_event("plan_change_rejected", "completed", detail={"proposal_id": proposal_id})
        publish_event_safe("plan_change_rejected", {"proposal_id": proposal_id})
        return PlanChangeProposalService._to_read(record)

    @staticmethod
    async def apply(db: AsyncSession, proposal_id: str) -> PlanChangeProposalReadSchema:
        record = await PlanChangeProposalService._load(db, proposal_id)
        if record.status == "proposed":
            raise ConflictError(
                resource="plan_change_proposal",
                message="Plan change cannot apply before approval",
                error_code="plan_change_not_approved",
            )
        if record.status == "rejected":
            raise ConflictError(
                resource="plan_change_proposal",
                message="Rejected proposal cannot apply",
                error_code="plan_change_rejected",
            )
        if record.status == "applied":
            return PlanChangeProposalService._to_read(record)

        plan = await PlanChangeProposalService._get_active_plan(db, record.plan_id)
        if plan is None:
            raise NotFoundError(resource="plan", message="Active plan not found")

        pre_errors = PlanChangeValidator.validate_change_set(record.change_set)
        if pre_errors:
            record.status = "failed"
            record.rejection_reason = "; ".join(pre_errors)[:2000]
            await db.commit()
            await db.refresh(record)
            raise ConflictError(
                resource="plan_change_proposal",
                message="Plan change validation failed",
                details={"errors": pre_errors},
                error_code="plan_change_validation_failed",
            )

        nodes_result = await db.execute(
            select(NodeRecord).where(
                NodeRecord.plan_id == record.plan_id,
                NodeRecord.status != "archived",
            )
        )
        db_nodes = list(nodes_result.scalars().all())
        import_nodes = PlanChangeProposalService._nodes_to_import_dicts(db_nodes)
        candidate, candidate_errors = PlanChangeValidator.build_candidate_plan(
            plan.goal, import_nodes, record.change_set,
        )
        if candidate_errors or candidate is None:
            record.status = "failed"
            record.rejection_reason = "; ".join(candidate_errors)[:2000]
            await db.commit()
            await db.refresh(record)
            raise ConflictError(
                resource="plan_change_proposal",
                message="Plan change validation failed",
                details={"errors": candidate_errors},
                error_code="plan_change_validation_failed",
            )

        try:
            for op in record.change_set:
                await PlanChangeProposalService._apply_operation_to_db(
                    db, record.plan_id, op, db_nodes,
                )
            await PlanService._validate_graph(db, record.plan_id)
            await PlanService._validate_interfaces_for_plan(db, record.plan_id)
            record.status = "applied"
            await db.commit()
            await PlanService._refresh_current_plan_file(db, record.plan_id)
            log_event("plan_change_applied", "completed", detail={"proposal_id": proposal_id})
            publish_event_safe("plan_change_applied", {"proposal_id": proposal_id})
        except Exception as exc:
            await db.rollback()
            record = await PlanChangeProposalService._load(db, proposal_id)
            record.status = "failed"
            record.rejection_reason = str(exc)[:2000]
            await db.commit()
            raise ConflictError(
                resource="plan_change_proposal",
                message="Plan change apply failed",
                details={"reason": str(exc)},
                error_code="plan_change_apply_failed",
            ) from exc

        await db.refresh(record)
        return PlanChangeProposalService._to_read(record)

    @staticmethod
    def _nodes_to_import_dicts(nodes: list[NodeRecord]) -> list[dict]:
        return [
            {
                "id": n.plan_node_id,
                "title": n.title,
                "goal": n.goal,
                "node_type": n.node_type,
                "depends_on": n.depends_on,
                "files": n.files,
                "tests": n.tests,
                "metrics": n.metrics,
                "constraints": n.constraints,
                "review_checks": n.review_checks,
                "expected_outputs": n.expected_outputs,
                "interfaces": n.interfaces if isinstance(n.interfaces, dict) else {},
            }
            for n in nodes
        ]

    @staticmethod
    async def _apply_operation_to_db(
        db: AsyncSession,
        plan_id: str,
        op: dict,
        db_nodes: list[NodeRecord],
    ) -> None:
        operation = op.get("operation")
        node_plan_id = op.get("node_id")
        fields = op.get("fields") or {}
        if operation != "update_node" or not node_plan_id:
            raise ValueError(f"Unsupported operation: {operation}")
        node = next((n for n in db_nodes if n.plan_node_id == node_plan_id), None)
        if node is None:
            raise ValueError(f"Node {node_plan_id} not found")
        field_errors = PlanChangeValidator.validate_allowed_fields(fields)
        if field_errors:
            raise ValueError("; ".join(field_errors))
        from bridle.engine.plan_change_validator import ALLOWED_FIELD_KEYS

        for key, value in fields.items():
            if key in ALLOWED_FIELD_KEYS:
                setattr(node, key, value)

    @staticmethod
    async def _get_active_plan(db: AsyncSession, plan_id: str) -> PlanRecord | None:
        result = await db.execute(
            select(PlanRecord).where(PlanRecord.id == plan_id, PlanRecord.status == "active")
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _load(db: AsyncSession, proposal_id: str) -> PlanChangeProposalRecord:
        result = await db.execute(
            select(PlanChangeProposalRecord).where(PlanChangeProposalRecord.id == proposal_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundError(resource="plan_change_proposal", message="Proposal not found")
        return record

    @staticmethod
    def _to_read(record: PlanChangeProposalRecord) -> PlanChangeProposalReadSchema:
        return PlanChangeProposalReadSchema(
            proposal_id=record.id,
            plan_id=record.plan_id,
            proposal_type=record.proposal_type,
            change_set=record.change_set,
            risk_level=record.risk_level,
            requires_human_review=record.requires_human_review,
            status=record.status,
            created_at=record.created_at,
            rejection_reason=record.rejection_reason,
        )
