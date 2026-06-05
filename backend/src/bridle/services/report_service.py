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
from bridle.services.node_agent_run_service import NodeAgentRunService

_AGENT_FAILED_STATUSES = frozenset({"failed", "timed_out", "blocked", "failed_retryable"})


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

        agent_runs = [run.model_dump() for run in await NodeAgentRunService.list_by_node(db, node_id)]

        legacy_run_count = len(runs)
        legacy_completed_runs = sum(1 for r in runs if r.status == "completed")
        legacy_failed_runs = sum(1 for r in runs if r.status == "failed")
        agent_run_count = len(agent_runs)
        agent_completed_runs = sum(1 for r in agent_runs if r.get("status") == "completed")
        agent_failed_runs = sum(
            1 for r in agent_runs if r.get("status") in _AGENT_FAILED_STATUSES
        )

        # Last successful run for baseline
        baseline = None
        for run in runs:
            if run.status == "completed":
                baseline = run
                break

        return {
            "node": node_schema.model_dump(),
            "runs": [r.model_dump() for r in runs],
            "agent_runs": agent_runs,
            "evidences": [e.model_dump() for e in evidences],
            "baseline_run": baseline.model_dump() if baseline else None,
            "summary": {
                "total_runs": legacy_run_count + agent_run_count,
                "completed_runs": legacy_completed_runs + agent_completed_runs,
                "failed_runs": legacy_failed_runs + agent_failed_runs,
                "legacy_run_count": legacy_run_count,
                "legacy_completed_runs": legacy_completed_runs,
                "legacy_failed_runs": legacy_failed_runs,
                "agent_run_count": agent_run_count,
                "agent_completed_runs": agent_completed_runs,
                "agent_failed_runs": agent_failed_runs,
                "evidence_count": len(evidences),
                "missing_evidence_count": sum(1 for e in evidences if e.status == "missing_evidence"),
            },
        }
