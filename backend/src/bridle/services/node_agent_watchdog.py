"""NodeAgentWatchdog — close stale, blocked, and hard-timed-out node agent runs."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.coding_config import ACTIVE_RUN_STATUSES, CODING_CONFIG
from bridle.events.bus import publish_event_safe
from bridle.utils.datetime_util import to_naive_utc, utc_now_naive
from bridle.logging.jsonl import log_event
from bridle.models.node import NodeRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.node_agent_run_service import NodeAgentRunService

logger = logging.getLogger("bridle")


class NodeAgentWatchdog:
    @staticmethod
    async def scan_and_close_stale(db: AsyncSession) -> int:
        now = utc_now_naive()
        stale_cutoff = now - timedelta(seconds=CODING_CONFIG.stale_after_seconds)
        hard_cutoff = now - timedelta(seconds=CODING_CONFIG.hard_timeout_seconds)
        blocked_cutoff = now - timedelta(seconds=CODING_CONFIG.blocked_timeout_seconds)

        result = await db.execute(
            select(NodeAgentRunRecord).where(NodeAgentRunRecord.status.in_(ACTIVE_RUN_STATUSES))
        )
        runs = list(result.scalars().all())
        closed = 0

        for run in runs:
            reason: str | None = None
            last_hb = to_naive_utc(run.last_heartbeat_at)
            timeout_at = to_naive_utc(run.timeout_at)
            started_at = to_naive_utc(run.started_at)
            if run.status == "blocked" and last_hb and last_hb < blocked_cutoff:
                await NodeAgentWatchdog._mark_blocked(db, run, now)
                closed += 1
                continue
            if timeout_at and timeout_at < now:
                reason = "hard_timeout"
            elif last_hb and last_hb < stale_cutoff:
                reason = "heartbeat_stale"
            elif started_at and started_at < hard_cutoff and last_hb is None:
                reason = "hard_timeout"
            if reason:
                await NodeAgentWatchdog._mark_timed_out(db, run, now, reason)
                closed += 1

        if closed:
            await db.commit()
        return closed

    @staticmethod
    async def _mark_timed_out(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        now,
        reason: str,
    ) -> None:
        last_phase = run.phase
        run.status = "timed_out"
        run.blocked_reason = reason
        run.finished_at = now
        if run.started_at:
            run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        await NodeAgentRunService.release_lock(db, run.node_id)
        log_event(
            "node_agent_run_timed_out",
            "completed",
            node_id=run.node_id,
            run_id=run.id,
            detail={
                "event": "node_agent_run_closed",
                "status": "timed_out",
                "reason": reason,
                "plan_node_id": run.plan_node_id,
                "last_phase": last_phase,
                "recommended_next_action": "retry_or_human_review",
            },
        )

    @staticmethod
    async def _mark_blocked(
        db: AsyncSession,
        run: NodeAgentRunRecord,
        now,
    ) -> None:
        run.status = "blocked"
        run.finished_at = now
        node_result = await db.execute(select(NodeRecord).where(NodeRecord.id == run.node_id))
        node = node_result.scalar_one_or_none()
        if node is not None:
            old_status = node.status
            node.status = "needs_review"
            if old_status != node.status:
                publish_event_safe(
                    "node_status_changed",
                    {
                        "node_id": node.id,
                        "plan_node_id": node.plan_node_id,
                        "old_status": old_status,
                        "new_status": node.status,
                    },
                )
        await NodeAgentRunService.release_lock(db, run.node_id)
        log_event(
            "node_agent_run_blocked",
            "completed",
            node_id=run.node_id,
            run_id=run.id,
            detail={"reason": "blocked_timeout"},
        )
        publish_event_safe(
            "node_agent_run_updated",
            {
                "run_id": run.id,
                "node_id": run.node_id,
                "status": run.status,
                "phase": run.phase,
            },
        )
