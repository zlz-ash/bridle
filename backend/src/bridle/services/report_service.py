"""Report service — generate node reports as JSON."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.evidence import EvidenceRecord
from bridle.models.node import NodeRecord
from bridle.models.run import RunRecord
from bridle.schemas.evidence import EvidenceReadSchema
from bridle.schemas.node import NodeReadSchema
from bridle.schemas.run import RunReadSchema


class ReportService:
    @staticmethod
    async def generate_node_report(db: AsyncSession, node_id: str) -> dict | None:
        """Generate a JSON report for a node: its metadata, runs, and evidence."""
        # Node
        result = await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        node = result.scalar_one_or_none()
        if node is None:
            return None
        node_schema = NodeReadSchema.model_validate(node)

        # Runs
        result = await db.execute(
            select(RunRecord).where(RunRecord.node_id == node_id).order_by(RunRecord.started_at.desc())
        )
        runs = [RunReadSchema.model_validate(r) for r in result.scalars().all()]

        # Evidences
        result = await db.execute(
            select(EvidenceRecord).where(EvidenceRecord.node_id == node_id).order_by(EvidenceRecord.created_at)
        )
        evidences = [EvidenceReadSchema.model_validate(e) for e in result.scalars().all()]

        # Last successful run for baseline
        baseline = None
        for run in runs:
            if run.status == "completed":
                baseline = run
                break

        return {
            "node": node_schema.model_dump(),
            "runs": [r.model_dump() for r in runs],
            "evidences": [e.model_dump() for e in evidences],
            "baseline_run": baseline.model_dump() if baseline else None,
            "summary": {
                "total_runs": len(runs),
                "completed_runs": sum(1 for r in runs if r.status == "completed"),
                "failed_runs": sum(1 for r in runs if r.status == "failed"),
                "evidence_count": len(evidences),
                "missing_evidence_count": sum(1 for e in evidences if e.status == "missing_evidence"),
            },
        }
