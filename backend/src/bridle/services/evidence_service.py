"""Evidence service — business logic for evidence records."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.evidence import EvidenceRecord
from bridle.schemas.evidence import EvidenceReadSchema


class EvidenceService:
    @staticmethod
    async def create(
        db: AsyncSession,
        run_id: str,
        node_id: str,
        evidence_type: str,
        content: dict | list,
        status: str = "collected",
    ) -> EvidenceReadSchema:
        ev = EvidenceRecord(
            run_id=run_id,
            node_id=node_id,
            evidence_type=evidence_type,
            content=content,
            status=status,
        )
        db.add(ev)
        await db.commit()
        await db.refresh(ev)
        return EvidenceReadSchema.model_validate(ev)

    @staticmethod
    async def list_by_run(db: AsyncSession, run_id: str) -> list[EvidenceReadSchema]:
        result = await db.execute(
            select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(EvidenceRecord.created_at)
        )
        return [EvidenceReadSchema.model_validate(r) for r in result.scalars().all()]

    @staticmethod
    async def list_by_node(db: AsyncSession, node_id: str) -> list[EvidenceReadSchema]:
        result = await db.execute(
            select(EvidenceRecord).where(EvidenceRecord.node_id == node_id).order_by(EvidenceRecord.created_at)
        )
        return [EvidenceReadSchema.model_validate(r) for r in result.scalars().all()]
