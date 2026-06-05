"""NodeEligibilityService — compute eligible and blocked nodes for coding sessions."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.coding_config import ACTIVE_RUN_STATUSES, ELIGIBLE_NODE_STATUSES
from bridle.engine.blocker import Blocker
from bridle.logging.jsonl import log_event
from bridle.models.node import NodeRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.models.plan import PlanRecord

logger = logging.getLogger("bridle")

MAX_NODE_SELECT_ATTEMPTS = 2


def complexity_block_reason(issues: list[str]) -> str:
    """Map complexity validation issues to eligibility blocked reason."""
    if any(str(i).startswith("node_incomplete:") for i in issues):
        return "node_incomplete"
    if any(str(i).startswith("node_too_complex:") for i in issues):
        return "node_too_complex"
    if any(str(i).startswith("node_too_granular:") for i in issues):
        return "node_too_granular"
    return "node_too_complex"


@dataclass
class EligibleNode:
    node_id: str
    plan_node_id: str
    status: str
    title: str


@dataclass
class BlockedNode:
    node_id: str
    plan_node_id: str
    status: str
    reason: str
    blocked_by: list[str]


class NodeEligibilityService:
    @staticmethod
    async def compute(
        db: AsyncSession,
        plan_id: str,
        *,
        session_id: str | None = None,
    ) -> tuple[list[EligibleNode], list[BlockedNode]]:
        plan = await NodeEligibilityService._get_active_plan(db, plan_id)
        if plan is None:
            return [], []

        nodes = await NodeEligibilityService._list_plan_nodes(db, plan_id)
        completed_ids = {n.plan_node_id for n in nodes if n.status == "completed"}
        running_node_ids = await NodeEligibilityService._nodes_with_active_runs(db, plan_id)
        prior_run_counts = await NodeEligibilityService._prior_run_counts(db, plan_id)

        eligible: list[EligibleNode] = []
        blocked: list[BlockedNode] = []

        for node in nodes:
            if node.status == "completed":
                continue
            if node.status == "archived":
                blocked.append(BlockedNode(
                    node_id=node.id,
                    plan_node_id=node.plan_node_id,
                    status=node.status,
                    reason="node_archived",
                    blocked_by=[],
                ))
                continue

            blockers = NodeEligibilityService._eligibility_blockers(
                node,
                completed_ids,
                running_node_ids,
                prior_run_counts.get(node.id, 0),
            )
            if blockers:
                blocked.append(BlockedNode(
                    node_id=node.id,
                    plan_node_id=node.plan_node_id,
                    status=node.status,
                    reason=blockers[0],
                    blocked_by=blockers[1],
                ))
            else:
                eligible.append(EligibleNode(
                    node_id=node.id,
                    plan_node_id=node.plan_node_id,
                    status=node.status,
                    title=node.title,
                ))

        log_event(
            "eligible_nodes_computed",
            "completed",
            detail={
                "plan_id": plan_id,
                "session_id": session_id,
                "eligible_count": len(eligible),
                "blocked_count": len(blocked),
            },
        )
        return eligible, blocked

    @staticmethod
    def _eligibility_blockers(
        node: NodeRecord,
        completed_ids: set[str],
        running_node_ids: set[str],
        prior_run_count: int = 0,
    ) -> tuple[str, list[str]] | None:
        if prior_run_count >= MAX_NODE_SELECT_ATTEMPTS:
            return ("node_attempts_exhausted", [])

        if node.status not in ELIGIBLE_NODE_STATUSES:
            if node.status == "blocked":
                complexity = node.metrics.get("complexity") if isinstance(node.metrics, dict) else None
                if isinstance(complexity, dict) and complexity.get("ok") is False:
                    issues = list(complexity.get("issues") or [])
                    return (complexity_block_reason(issues), issues)
            return ("node_status_not_eligible", [])

        unmet = [dep for dep in node.depends_on if dep not in completed_ids]
        if unmet:
            return ("dependency_not_completed", unmet)

        block = Blocker.check(node, completed_ids)
        if block.blocked:
            return ("node_blocked", [block.reason] if block.reason else [])

        if node.id in running_node_ids:
            return ("node_already_running", [])

        return None

    @staticmethod
    async def is_node_eligible(db: AsyncSession, plan_id: str, node_id: str) -> tuple[bool, str, list[str]]:
        eligible, blocked = await NodeEligibilityService.compute(db, plan_id)
        for e in eligible:
            if e.node_id == node_id:
                return True, "", []
        for b in blocked:
            if b.node_id == node_id:
                return False, b.reason, b.blocked_by
        return False, "node_not_found", []

    @staticmethod
    async def _get_active_plan(db: AsyncSession, plan_id: str) -> PlanRecord | None:
        result = await db.execute(
            select(PlanRecord).where(
                PlanRecord.id == plan_id,
                PlanRecord.status == "active",
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _list_plan_nodes(db: AsyncSession, plan_id: str) -> list[NodeRecord]:
        result = await db.execute(
            select(NodeRecord).where(NodeRecord.plan_id == plan_id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def _prior_run_counts(db: AsyncSession, plan_id: str) -> dict[str, int]:
        result = await db.execute(
            select(NodeAgentRunRecord.node_id, func.count(NodeAgentRunRecord.id))
            .join(NodeRecord, NodeAgentRunRecord.node_id == NodeRecord.id)
            .where(NodeRecord.plan_id == plan_id)
            .group_by(NodeAgentRunRecord.node_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    @staticmethod
    async def count_prior_runs(db: AsyncSession, node_id: str) -> int:
        result = await db.execute(
            select(func.count(NodeAgentRunRecord.id)).where(
                NodeAgentRunRecord.node_id == node_id,
            )
        )
        return int(result.scalar() or 0)

    @staticmethod
    async def _nodes_with_active_runs(db: AsyncSession, plan_id: str) -> set[str]:
        result = await db.execute(
            select(NodeAgentRunRecord)
            .join(NodeRecord, NodeAgentRunRecord.node_id == NodeRecord.id)
            .where(
                NodeRecord.plan_id == plan_id,
                NodeAgentRunRecord.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        return {r.node_id for r in result.scalars().all()}
