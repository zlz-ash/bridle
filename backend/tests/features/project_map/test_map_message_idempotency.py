from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.mailbox import AgentAddress, MailboxResult, MailEnvelope
from bridle.agent.runtime.persistence import add_runtime_input_delivery
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_map_agent import ProjectMapAgent, ProjectMapAgentState
from bridle.features.project_map.store import ProjectPlanStore
from bridle.logging.facade import LoggingFacade
from bridle.models.agent_runtime import RuntimeInputDeliveryRecord
from tests.agent.runtime.test_agent_runtime_host import _grant
from tests.agent.runtime.test_change_outbox import CapturingSink
from tests.agent.runtime.test_persistent_mailbox import MutableClock


def _receipt_ids(store: ProjectPlanStore) -> set[str]:
    with closing(sqlite3.connect(store.database_path)) as connection:
        rows = connection.execute(
            "SELECT message_id FROM map_applied_messages ORDER BY message_id"
        ).fetchall()
    return {str(row[0]) for row in rows}


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


def _mailbox(
    root: Path,
    project_id: str,
    consumer_id: str,
    *,
    lease_seconds: float = 1,
    clock=None,
) -> PersistentMailbox:
    return PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id=consumer_id,
        lease_seconds=lease_seconds,
        retry_base_seconds=0.01,
        retry_max_seconds=0.01,
        clock=clock,
        default_target=AgentAddress(project_id, "map-runtime", 1),
    )


def _host(db: AsyncSession):
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    return AgentRuntimeHost(sessions), sessions


async def _start_hosted_agent(
    host: AgentRuntimeHost,
    agent: ProjectMapAgent,
    mailbox: PersistentMailbox,
):
    handle = await host.create_runtime(
        role=RuntimeRole.MAP,
        project_id=agent.project_id,
        agent_id="map-runtime",
        generation=agent.generation,
        grant=_grant(agent.project_id),
        task_factory=lambda runtime: agent.run(runtime, host),
        mailbox=mailbox,
    )
    await host.transition(handle, RuntimeState.RUNNING, reason="map_handler_started")
    agent.activate()
    return handle


def _enqueue(mailbox: PersistentMailbox, message_id: str, *, message_type: str = "CodeChanged") -> None:
    target = AgentAddress(mailbox.project_id, "map-runtime", 1)
    result = mailbox.enqueue(
        MailEnvelope(
            message_id,
            message_type,
            AgentAddress(mailbox.project_id, "change-outbox", 1),
            target,
            {"path": "a.py"},
        )
    )
    assert result.status == "inserted"


def _mail_status(mailbox: PersistentMailbox, message_id: str) -> str:
    with closing(sqlite3.connect(mailbox.database_path)) as connection:
        row = connection.execute(
            "SELECT status FROM mail_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_map_batch_merges_paths_and_records_all_receipts_in_one_transaction(
    test_workspace: Path,
) -> None:
    (test_workspace / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (test_workspace / "b.py").write_text("VALUE = 2\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="project-a")
    store.initialize()
    before = store.latest_change_seq()

    result = store.apply_code_changed_batch(
        [
            ("message-1", ["a.py", "a.py"]),
            ("message-2", ["a.py", "b.py"]),
        ]
    )

    assert result == {
        "applied_message_ids": ["message-1", "message-2"],
        "duplicate_message_ids": [],
        "refreshed_paths": ["a.py", "b.py"],
    }
    assert _receipt_ids(store) == {"message-1", "message-2"}
    after_first_apply = store.latest_change_seq()
    assert after_first_apply > before

    replay = store.apply_code_changed_batch(
        [("message-1", ["a.py"]), ("message-2", ["b.py"])]
    )
    assert replay["applied_message_ids"] == []
    assert replay["duplicate_message_ids"] == ["message-1", "message-2"]
    assert replay["refreshed_paths"] == []
    assert store.latest_change_seq() == after_first_apply


def test_map_transaction_failure_keeps_receipts_and_change_seq_unchanged(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (test_workspace / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="project-a")
    store.initialize()
    before = store.latest_change_seq()

    def fail_refresh(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("deterministic_store_failure")

    monkeypatch.setattr(store, "_refresh_code_paths_in_connection", fail_refresh)
    with pytest.raises(RuntimeError, match="deterministic_store_failure"):
        store.apply_code_changed_batch([("message-1", ["a.py"])])

    assert _receipt_ids(store) == set()
    assert store.latest_change_seq() == before


@pytest.mark.asyncio
async def test_commit_before_ack_redelivery_is_idempotent(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    project_id = "project-crash"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.initialize(scan_if_created=False)
    producer = _mailbox(test_workspace, project_id, "producer")
    _enqueue(producer, "crash-message")
    await producer.close()

    async def keep_running(_project_id: str, _generation: int, _version: int) -> bool:
        return False

    def crash_after_commit() -> None:
        raise RuntimeError("crash_after_commit")

    first_mailbox = _mailbox(test_workspace, project_id, "map-runtime-1")
    first = ProjectMapAgent(
        project_id,
        test_workspace,
        generation=1,
        mailbox=first_mailbox,
        retire_callback=keep_running,
        after_commit_hook=crash_after_commit,
    )
    host, _sessions = _host(db)
    first_handle = await _start_hosted_agent(host, first, first_mailbox)
    assert first_handle.task is not None
    with pytest.raises(RuntimeError, match="crash_after_commit"):
        await asyncio.wait_for(first_handle.task, timeout=2)
    after_commit_seq = store.latest_change_seq()
    assert first.state is ProjectMapAgentState.FAILED
    assert _receipt_ids(store) == {"crash-message"}
    assert _mail_status(first_mailbox, "crash-message") == "pending"
    await host.destroy(first_handle)

    async def retire(_project_id: str, _generation: int, _version: int) -> bool:
        return True

    second_mailbox = _mailbox(test_workspace, project_id, "map-runtime-2")
    second = ProjectMapAgent(
        project_id,
        test_workspace,
        generation=2,
        mailbox=second_mailbox,
        retire_callback=retire,
    )
    second_handle = await _start_hosted_agent(host, second, second_mailbox)
    await _wait_until(lambda: _mail_status(second_mailbox, "crash-message") == "delivered")
    assert store.latest_change_seq() == after_commit_seq
    await host.destroy(second_handle)


@pytest.mark.asyncio
async def test_lost_ack_lease_keeps_mail_and_does_not_repeat_map_effect(
    test_workspace: Path,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "project-lost-lease"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.initialize(scan_if_created=False)
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    mailbox = _mailbox(
        test_workspace,
        project_id,
        "map-runtime",
        lease_seconds=1,
        clock=clock,
    )
    _enqueue(mailbox, "lease-message")
    monkeypatch.setattr(
        mailbox,
        "ack",
        lambda message_id, _lease_token, *, target: MailboxResult(
            "lost_lease",
            message_id=message_id,
        ),
    )

    async def keep_running(_project_id: str, _generation: int, _version: int) -> bool:
        return False

    host, _sessions = _host(db)
    first_agent = ProjectMapAgent(
        project_id,
        test_workspace,
        generation=1,
        mailbox=mailbox,
        retire_callback=keep_running,
    )
    first_handle = await _start_hosted_agent(host, first_agent, mailbox)
    await _wait_until(lambda: first_agent.degraded)
    after_first = store.latest_change_seq()
    persisted = ProjectPlanStore.open_existing(test_workspace).overview()
    assert persisted["scan_status"] == "stale"
    assert persisted["readiness_reason"] == "mail_ack_lost_lease"
    assert _mail_status(mailbox, "lease-message") == "leased"
    await host.destroy(first_handle)

    clock.advance(2)
    recovery_mailbox = _mailbox(
        test_workspace,
        project_id,
        "map-runtime-recovery",
        clock=clock,
    )

    async def retire(_project_id: str, _generation: int, _version: int) -> bool:
        return True

    recovery_agent = ProjectMapAgent(
        project_id,
        test_workspace,
        generation=2,
        mailbox=recovery_mailbox,
        retire_callback=retire,
    )
    recovery_handle = await _start_hosted_agent(host, recovery_agent, recovery_mailbox)
    await _wait_until(lambda: _mail_status(recovery_mailbox, "lease-message") == "delivered")
    assert store.latest_change_seq() == after_first
    assert _receipt_ids(store) == {"lease-message"}
    await host.destroy(recovery_handle)


@pytest.mark.asyncio
async def test_map_transaction_failure_keeps_mail_and_marks_degraded(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    db: AsyncSession,
) -> None:
    project_id = "project-failure"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.initialize(scan_if_created=False)
    before = store.latest_change_seq()
    mailbox = _mailbox(test_workspace, project_id, "map-runtime")
    _enqueue(mailbox, "failure-message")

    def fail(_self: ProjectPlanStore, _messages: object) -> dict[str, list[str]]:
        raise RuntimeError("deterministic_store_failure")

    monkeypatch.setattr(ProjectPlanStore, "apply_code_changed_batch", fail)

    async def keep_running(_project_id: str, _generation: int, _version: int) -> bool:
        return False

    host, sessions = _host(db)
    parent_release = asyncio.Event()
    parent_started = asyncio.Event()

    async def parent_work(parent_handle) -> None:
        parent_started.set()
        await parent_release.wait()
        async with sessions() as session:
            await add_runtime_input_delivery(
                session,
                message_id="parent-work-message",
                session_message_id="parent-session-message",
                project_id=project_id,
                session_id="parent-session",
                target_address=parent_handle.spec.address.to_uri(),
                target_agent_id=parent_handle.spec.agent_id,
                target_generation=parent_handle.spec.generation,
            )
            await session.commit()

    parent_handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id=project_id,
        session_id="parent-session",
        agent_id="parent-runtime",
        generation=1,
        grant=_grant(project_id),
        task_factory=parent_work,
    )
    await host.transition(parent_handle, RuntimeState.RUNNING, reason="parent_work_started")
    await asyncio.wait_for(parent_started.wait(), timeout=1)

    agent = ProjectMapAgent(
        project_id,
        test_workspace,
        generation=1,
        mailbox=mailbox,
        retire_callback=keep_running,
    )
    map_handle = await _start_hosted_agent(host, agent, mailbox)
    await _wait_until(lambda: agent.degraded)
    assert not parent_handle.task.done()
    parent_release.set()
    await asyncio.wait_for(parent_handle.task, timeout=1)
    async with sessions() as session:
        persisted_parent_work = await session.scalar(
            select(RuntimeInputDeliveryRecord).where(
                RuntimeInputDeliveryRecord.message_id == "parent-work-message"
            )
        )
    assert persisted_parent_work is not None
    assert persisted_parent_work.project_id == project_id
    assert _mail_status(mailbox, "failure-message") != "delivered"
    assert _receipt_ids(store) == set()
    assert store.latest_change_seq() == before
    persisted = ProjectPlanStore.open_existing(test_workspace).overview()
    assert persisted["scan_status"] == "stale"
    assert persisted["readiness_reason"] == "RuntimeError"
    await host.destroy(map_handle)
    await host.destroy(parent_handle)
    assert _mail_status(mailbox, "failure-message") in {"pending", "retry_wait"}


@pytest.mark.asyncio
async def test_non_code_changed_is_not_applied_or_acked(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    project_id = "project-other"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.initialize(scan_if_created=False)
    before = store.latest_change_seq()
    mailbox = _mailbox(test_workspace, project_id, "map-runtime")
    _enqueue(mailbox, "other-message", message_type="Other")

    async def keep_running(_project_id: str, _generation: int, _version: int) -> bool:
        return False

    agent = ProjectMapAgent(
        project_id,
        test_workspace,
        generation=1,
        mailbox=mailbox,
        retire_callback=keep_running,
    )
    host, _sessions = _host(db)
    handle = await _start_hosted_agent(host, agent, mailbox)
    await _wait_until(lambda: agent.degraded)
    assert _mail_status(mailbox, "other-message") != "delivered"
    assert _receipt_ids(store) == set()
    assert store.latest_change_seq() == before
    persisted = ProjectPlanStore.open_existing(test_workspace).overview()
    assert persisted["scan_status"] == "stale"
    assert persisted["readiness_reason"] == "unsupported_map_message"
    await host.destroy(handle)
    assert _mail_status(mailbox, "other-message") in {"pending", "retry_wait"}


@pytest.mark.asyncio
async def test_degraded_persistence_failure_has_distinct_safe_log(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "project-degraded-log"
    ProjectPlanStore(test_workspace, project_id=project_id).initialize(scan_if_created=False)
    mailbox = _mailbox(test_workspace, project_id, "map-runtime")
    sink = CapturingSink([])

    def fail_persist(_self: ProjectPlanStore, *, reason: str):
        del reason
        raise RuntimeError("secret-persistence-detail")

    monkeypatch.setattr(ProjectPlanStore, "mark_map_degraded", fail_persist)

    async def retire(_project_id: str, _generation: int, _version: int) -> bool:
        return True

    agent = ProjectMapAgent(
        project_id,
        test_workspace,
        generation=1,
        mailbox=mailbox,
        retire_callback=retire,
        logging_facade=LoggingFacade(sinks=[sink]),
    )
    await agent._mark_degraded("RuntimeError", message_id="degraded-message")
    records = [event.to_dict() for event in sink.events]
    failure = next(
        record for record in records if record["action"] == "map.degraded_persist_failed"
    )
    assert failure["status"] == "failed"
    assert failure["error_code"] == "RuntimeError"
    assert failure["message_id"] == "degraded-message"
    assert "secret-persistence-detail" not in str(failure)
    assert any(record["action"] == "map.degraded" for record in records)
    await mailbox.close()
