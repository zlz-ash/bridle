"""Ensure main-agent containers exist for active coding sessions."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import is_test_mode
from bridle.api.errors import ConflictError
from bridle.config import get_config
from bridle.engine.container_runner import ContainerResult
from bridle.logging.jsonl import log_event
from bridle.models.agent_coding_session import AgentCodingSessionRecord
from bridle.services.agent_coding_session_service import AgentCodingSessionService
from bridle.services.main_agent_container_service import MainAgentContainerService

logger = logging.getLogger("bridle")


class SessionReconciler:
    @staticmethod
    def _container_service() -> MainAgentContainerService:
        return MainAgentContainerService(get_config().workspace)

    @staticmethod
    def _should_skip_reconcile() -> bool:
        return is_test_mode() or os.getenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "").strip() == "1"

    @staticmethod
    def _container_is_running(
        container_service: MainAgentContainerService,
        *,
        session_id: str,
    ) -> bool | None:
        metadata = container_service.read_for_session(session_id)
        if metadata is None:
            return None
        container_id = metadata.get("container_id")
        if not container_id:
            return False
        try:
            inspected = container_service.runner.inspect(str(container_id))
        except Exception:
            logger.warning(
                "main_agent_container_inspect_failed",
                extra={"detail": {"session_id": session_id, "container_id": container_id}},
            )
            return None
        if inspected.health == "missing":
            return False
        return SessionReconciler._inspect_is_running(inspected)

    @staticmethod
    def _inspect_is_running(inspected: ContainerResult) -> bool:
        if inspected.status != "running":
            return False
        return inspected.health not in {"unhealthy", "exited", "failed", "missing"}

    @staticmethod
    def _cleanup_dead_container(
        container_service: MainAgentContainerService,
        container_id: str,
    ) -> None:
        runner = container_service.runner
        try:
            runner.stop(container_id)
        except Exception:
            pass
        remove = getattr(runner, "remove", None)
        if callable(remove):
            try:
                remove(container_id)
            except Exception:
                pass

    @staticmethod
    def reconcile_session_sync(
        *,
        session_id: str,
        plan_id: str,
        container_service: MainAgentContainerService,
    ) -> dict[str, Any]:
        running = SessionReconciler._container_is_running(container_service, session_id=session_id)
        if running is True:
            return {"session_id": session_id, "action": "already_running"}

        metadata = container_service.read_for_session(session_id)
        if metadata and metadata.get("container_id"):
            SessionReconciler._cleanup_dead_container(
                container_service,
                str(metadata["container_id"]),
            )

        try:
            container_service.record_for_session(session_id=session_id, plan_id=plan_id)
        except (ValueError, RuntimeError) as exc:
            return {
                "session_id": session_id,
                "action": "failed",
                "error": str(exc),
            }
        return {"session_id": session_id, "action": "restarted"}

    @staticmethod
    async def reconcile_on_startup(db: AsyncSession) -> dict[str, Any]:
        if SessionReconciler._should_skip_reconcile():
            return {"skipped": True, "already_running": [], "restarted": [], "failed": []}

        result = await db.execute(
            select(AgentCodingSessionRecord).where(
                AgentCodingSessionRecord.status == "active",
                AgentCodingSessionRecord.mode == "coding",
            )
        )
        sessions = list(result.scalars().all())
        container_service = SessionReconciler._container_service()
        loop = asyncio.get_running_loop()

        already_running: list[str] = []
        restarted: list[str] = []
        failed: list[dict[str, str]] = []

        for session in sessions:
            outcome = await loop.run_in_executor(
                None,
                lambda sid=session.id, pid=session.plan_id: SessionReconciler.reconcile_session_sync(
                    session_id=sid,
                    plan_id=pid,
                    container_service=container_service,
                ),
            )
            action = outcome.get("action")
            if action == "already_running":
                already_running.append(session.id)
            elif action == "restarted":
                restarted.append(session.id)
            elif action == "failed":
                reason = f"reconcile_failed:{outcome.get('error', 'unknown')}"
                failed.append({"session_id": session.id, "reason": reason})
                await AgentCodingSessionService.fail_session(db, session.id, reason=reason)

        summary = {
            "already_running": already_running,
            "restarted": restarted,
            "failed": failed,
        }
        log_event(
            "session_reconciled",
            "completed",
            detail=summary,
        )
        return summary

    @staticmethod
    async def ensure_main_agent_alive(session_id: str, db: AsyncSession) -> None:
        if SessionReconciler._should_skip_reconcile():
            return

        result = await db.execute(
            select(AgentCodingSessionRecord).where(AgentCodingSessionRecord.id == session_id)
        )
        session = result.scalar_one_or_none()
        if session is None or session.mode != "coding" or session.status != "active":
            return

        container_service = SessionReconciler._container_service()
        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(
            None,
            lambda: SessionReconciler.reconcile_session_sync(
                session_id=session.id,
                plan_id=session.plan_id,
                container_service=container_service,
            ),
        )
        if outcome.get("action") == "failed":
            raise ConflictError(
                resource="coding_session",
                message="Main agent container is unavailable",
                details={"session_id": session_id, "reason": outcome.get("error")},
                error_code="main_agent_unavailable",
            )
