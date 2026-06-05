"""SessionReconciler — main-agent container liveness on startup and on demand."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.engine.container_runner import ContainerResult
from bridle.models.agent_coding_session import AgentCodingSessionRecord
from bridle.models.plan import PlanRecord
from bridle.models.task import TaskRecord
from bridle.services.session_reconciler import SessionReconciler


async def _active_coding_session(db: AsyncSession) -> AgentCodingSessionRecord:
    task = TaskRecord(title="Reconcile", status="planned")
    db.add(task)
    await db.flush()
    plan = PlanRecord(task_id=task.id, goal="G", status="active")
    db.add(plan)
    await db.flush()
    session = AgentCodingSessionRecord(plan_id=plan.id, status="active", mode="coding")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


def _running_inspect() -> ContainerResult:
    return ContainerResult(
        container_id="cid-1",
        name="main-agent-s1",
        status="running",
        network_mode="bridge",
        health="healthy",
    )


def _stopped_inspect() -> ContainerResult:
    return ContainerResult(
        container_id="cid-1",
        name="main-agent-s1",
        status="failed",
        network_mode="bridge",
        health="exited",
    )


class TestSessionReconciler:
    @pytest.mark.asyncio
    async def test_alive_container_skips_record_for_session(self, db: AsyncSession) -> None:
        session = await _active_coding_session(db)
        svc = MagicMock()
        svc.read_for_session.return_value = {"container_id": "cid-1", "plan_id": session.plan_id}
        svc.runner.inspect.return_value = _running_inspect()

        outcome = SessionReconciler.reconcile_session_sync(
            session_id=session.id,
            plan_id=session.plan_id,
            container_service=svc,
        )

        assert outcome["action"] == "already_running"
        svc.record_for_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_dead_container_restarts(self, db: AsyncSession) -> None:
        session = await _active_coding_session(db)
        svc = MagicMock()
        svc.read_for_session.return_value = {"container_id": "cid-1", "plan_id": session.plan_id}
        svc.runner.inspect.return_value = _stopped_inspect()
        svc.record_for_session.return_value = {"container_id": "cid-2"}

        outcome = SessionReconciler.reconcile_session_sync(
            session_id=session.id,
            plan_id=session.plan_id,
            container_service=svc,
        )

        assert outcome["action"] == "restarted"
        svc.record_for_session.assert_called_once_with(
            session_id=session.id,
            plan_id=session.plan_id,
        )

    @pytest.mark.asyncio
    async def test_restart_failure_marks_session_failed_on_startup(self, db: AsyncSession) -> None:
        session = await _active_coding_session(db)
        svc = MagicMock()
        svc.read_for_session.return_value = None
        svc.record_for_session.side_effect = ValueError("image_missing")

        with patch(
            "bridle.services.session_reconciler.SessionReconciler._container_service",
            return_value=svc,
        ), patch.object(SessionReconciler, "_should_skip_reconcile", return_value=False):
            summary = await SessionReconciler.reconcile_on_startup(db)

        assert summary["failed"]
        await db.refresh(session)
        assert session.status == "failed"

    @pytest.mark.asyncio
    async def test_reconcile_on_startup_restarts_dead_sessions(self, db: AsyncSession) -> None:
        session = await _active_coding_session(db)
        svc = MagicMock()
        svc.read_for_session.return_value = {"container_id": "cid-1"}
        svc.runner.inspect.return_value = _stopped_inspect()
        svc.record_for_session.return_value = {"container_id": "cid-2"}

        with patch(
            "bridle.services.session_reconciler.SessionReconciler._container_service",
            return_value=svc,
        ), patch.object(SessionReconciler, "_should_skip_reconcile", return_value=False):
            summary = await SessionReconciler.reconcile_on_startup(db)

        assert summary["restarted"] == [session.id]
        svc.record_for_session.assert_called_once()

    def test_reconcile_after_serve_restart_keeps_running_container(self) -> None:
        svc = MagicMock()
        svc.read_for_session.return_value = {"container_id": "cid-live", "plan_id": "plan-1"}
        svc.runner.inspect.return_value = _running_inspect()

        outcome = SessionReconciler.reconcile_session_sync(
            session_id="s1",
            plan_id="plan-1",
            container_service=svc,
        )

        assert outcome["action"] == "already_running"
        svc.record_for_session.assert_not_called()
        svc.runner.stop.assert_not_called()

    def test_reconcile_cleans_dead_container_before_restart(self) -> None:
        svc = MagicMock()
        svc.read_for_session.return_value = {"container_id": "cid-dead", "plan_id": "plan-1"}
        svc.runner.inspect.return_value = _stopped_inspect()
        svc.record_for_session.return_value = {"container_id": "cid-new"}

        outcome = SessionReconciler.reconcile_session_sync(
            session_id="s1",
            plan_id="plan-1",
            container_service=svc,
        )

        assert outcome["action"] == "restarted"
        svc.runner.stop.assert_called_once_with("cid-dead")
        svc.runner.remove.assert_called_once_with("cid-dead")
        svc.record_for_session.assert_called_once_with(session_id="s1", plan_id="plan-1")
