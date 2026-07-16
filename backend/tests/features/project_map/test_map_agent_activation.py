from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime.change_outbox import (
    AtomicPatchCommitter,
    ChangeCorrelation,
    ChangeOutbox,
    ChangeOutboxForwarder,
)
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_registry import ProjectRuntimeRegistry
from bridle.features.project_map.store import ProjectPlanStore
from bridle.logging.facade import LoggingFacade
from tests.agent.runtime.test_agent_runtime_host import _database
from tests.agent.runtime.test_change_outbox import CapturingSink


def _initialize(root: Path, project_id: str) -> None:
    ProjectPlanStore(root, project_id=project_id).initialize(scan_if_created=False)


def _registry(
    db: AsyncSession,
    *,
    facade: LoggingFacade | None = None,
    retire_hook=None,
) -> ProjectRuntimeRegistry:
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    return ProjectRuntimeRegistry(
        runtime_host=AgentRuntimeHost(sessions, facade=facade),
        logging_facade=facade,
        retire_hook=retire_hook,
    )


async def _enqueue(
    root: Path,
    project_id: str,
    message_id: str,
    *,
    message_type: str = "CodeChanged",
    path: str = "a.py",
) -> None:
    target = AgentAddress(project_id, "map-runtime", 1)
    mailbox = PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id=f"producer-{message_id}",
    )
    result = mailbox.enqueue(
        MailEnvelope(
            message_id,
            message_type,
            AgentAddress(project_id, "change-outbox", 1),
            target,
            {"path": path},
        )
    )
    assert result.status in {"inserted", "existing"}
    await mailbox.close()


def _receipt_exists(root: Path, message_id: str) -> bool:
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        return connection.execute(
            "SELECT 1 FROM map_applied_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone() is not None


def _mail_status(root: Path, message_id: str) -> str | None:
    with closing(sqlite3.connect(root / ".bridle" / "mail.db")) as connection:
        row = connection.execute(
            "SELECT status FROM mail_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return None if row is None else str(row[0])


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


@pytest.mark.asyncio
async def test_concurrent_first_code_changed_starts_one_map_generation(
    test_workspace: Path,
) -> None:
    project_id = "project-a"
    _initialize(test_workspace, project_id)
    sink = CapturingSink([])
    facade = LoggingFacade(sinks=[sink])
    engine, sessions = await _database(test_workspace)
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    host = AgentRuntimeHost(sessions, facade=facade)
    try:
        registry = ProjectRuntimeRegistry(runtime_host=host, logging_facade=facade)
    except Exception:
        await engine.dispose()
        raise
    outbox = ChangeOutbox(test_workspace, project_id=project_id)
    committer = AtomicPatchCommitter(outbox)
    correlation = ChangeCorrelation("trace-a", project_id, "session-a", 1)
    first = committer.commit(
        "a.py", change_type="add", new_text="A = 1\n", correlation=correlation
    )
    assert first.intent is not None
    mailbox = PersistentMailbox(
        test_workspace / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id="forwarder",
    )

    results = ChangeOutboxForwarder(outbox, mailbox, poll_seconds=0.01).run_once()
    assert [result.status for result in results] == ["delivered"]
    assert registry.active_count == 0

    release = asyncio.Event()
    all_waiting = asyncio.Event()
    waiting = 0
    waiting_lock = asyncio.Lock()

    async def simultaneous_wake():
        nonlocal waiting
        async with waiting_lock:
            waiting += 1
            if waiting == 12:
                all_waiting.set()
        await release.wait()
        return await registry.wake(project_id, test_workspace)

    tasks = [asyncio.create_task(simultaneous_wake()) for _item in range(12)]
    await asyncio.wait_for(all_waiting.wait(), timeout=1)
    release.set()
    agents = await asyncio.gather(*tasks)
    assert all(agent is agents[0] for agent in agents)
    assert registry.generation(project_id) == 1
    assert agents[0].runtime_handle is not None
    assert agents[0].runtime_handle.spec.role is RuntimeRole.MAP
    assert sum(event.action == "map.runtime_created" for event in sink.events) == 1
    await _wait_until(lambda: _receipt_exists(test_workspace, first.intent.message_id))
    await _wait_until(lambda: registry.active_count == 0)
    await _wait_until(lambda: agents[0].runtime_handle.state is RuntimeState.DESTROYED)
    async with sessions() as session:
        record = await get_runtime_record(session, agents[0].runtime_handle.spec.runtime_id)
        assert record.runtime_type == RuntimeRole.MAP
        assert record.generation == 1
        assert record.status == RuntimeState.DESTROYED
    await mailbox.close()
    await registry.stop_all()
    await engine.dispose()


@pytest.mark.asyncio
async def test_pending_mail_on_restart_activates_one_map_generation(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    project_id = "project-restart"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    _initialize(test_workspace, project_id)
    await _enqueue(test_workspace, project_id, "restart-message")

    registry = _registry(db)
    agent = await registry.wake_if_pending(project_id, test_workspace)

    assert agent is not None
    await _wait_until(lambda: _mail_status(test_workspace, "restart-message") == "delivered")
    assert _receipt_exists(test_workspace, "restart-message")
    assert registry.generation(project_id) == 1
    await registry.stop_all()


@pytest.mark.asyncio
async def test_running_transition_failure_cleans_runtime_before_successor(
    test_workspace: Path,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "project-transition-retry"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    _initialize(test_workspace, project_id)
    await _enqueue(test_workspace, project_id, "transition-retry-message")
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    host = AgentRuntimeHost(sessions)
    registry = ProjectRuntimeRegistry(runtime_host=host)
    original_transition = host.transition
    failed = False

    async def fail_once(handle, target, *, reason: str):
        nonlocal failed
        from bridle.agent.runtime.agent_runtime import RuntimeState

        if target is RuntimeState.RUNNING and not failed:
            failed = True
            raise RuntimeError("injected_running_transition_failure")
        return await original_transition(handle, target, reason=reason)

    monkeypatch.setattr(host, "transition", fail_once)
    try:
        with pytest.raises(RuntimeError, match="injected_running_transition_failure"):
            await registry.wake(project_id, test_workspace)
        assert registry.active_count == 0
        assert host.active_handles() == ()

        successor = await registry.wake(project_id, test_workspace)
        assert successor.generation == 2
        await _wait_until(
            lambda: _mail_status(test_workspace, "transition-retry-message") == "delivered"
        )
        assert _receipt_exists(test_workspace, "transition-retry-message")
    finally:
        await registry.stop_all()


async def _exercise_retirement_window(
    test_workspace: Path,
    stage: str,
    db: AsyncSession,
) -> int:
    project_id = f"project-{stage}"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    _initialize(test_workspace, project_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    blocked = False

    async def hook(_project_id: str, current_stage: str) -> None:
        nonlocal blocked
        if current_stage == stage and not blocked:
            blocked = True
            entered.set()
            await release.wait()

    registry = _registry(db, retire_hook=hook)
    await registry.wake(project_id, test_workspace)
    await asyncio.wait_for(entered.wait(), timeout=1)
    await _enqueue(test_workspace, project_id, f"message-{stage}")
    wake = asyncio.create_task(registry.wake(project_id, test_workspace))
    release.set()
    agent = await asyncio.wait_for(wake, timeout=1)
    await _wait_until(lambda: _receipt_exists(test_workspace, f"message-{stage}"))
    generation = agent.generation
    await registry.stop_all()
    return generation


@pytest.mark.asyncio
async def test_message_between_empty_checks_is_claimed_without_destroy(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    assert await _exercise_retirement_window(test_workspace, "after_first_empty", db) == 1


@pytest.mark.asyncio
async def test_message_after_second_empty_before_generation_removal_keeps_current_generation(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    assert await _exercise_retirement_window(test_workspace, "after_second_empty", db) == 1


@pytest.mark.asyncio
async def test_message_after_generation_removal_starts_exactly_one_successor(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    assert await _exercise_retirement_window(test_workspace, "after_removal", db) == 2


def test_production_has_no_watcher_or_startup_scan_entry() -> None:
    backend = Path(__file__).resolve().parents[3]
    runtime = backend / "src" / "bridle" / "agent" / "runtime"
    assert not (backend / "src" / "bridle" / "features" / "project_map" / "watcher.py").exists()
    registry_source = (runtime / "project_registry.py").read_text(encoding="utf-8").lower()
    agent_source = (runtime / "project_map_agent.py").read_text(encoding="utf-8").lower()
    service_source = (
        backend / "src" / "bridle" / "features" / "projects" / "service.py"
    ).read_text(encoding="utf-8")
    assert "watcher" not in registry_source
    assert "watcher" not in agent_source
    assert ".ensure_started(" not in service_source
    assert "scan_if_created=False" in service_source


@pytest.mark.asyncio
async def test_map_lifecycle_logs_ten_actions_with_correlation_and_no_content(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    project_id = "project-logs"
    (test_workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    _initialize(test_workspace, project_id)
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.apply_code_changed_batch([("duplicate-message", [])])
    sink = CapturingSink([])
    facade = LoggingFacade(sinks=[sink])
    registry = _registry(db, facade=facade)
    await _enqueue(test_workspace, project_id, "duplicate-message")
    await registry.wake(project_id, test_workspace)
    await _wait_until(lambda: _mail_status(test_workspace, "duplicate-message") == "delivered")
    await _wait_until(lambda: registry.active_count == 0)
    await _enqueue(
        test_workspace,
        project_id,
        "unsupported-message",
        message_type="Other",
    )
    await registry.wake(project_id, test_workspace)
    await _wait_until(lambda: any(event.action == "map.degraded" for event in sink.events))
    await registry.stop_all()

    expected = {
        "map.wake_requested",
        "map.runtime_created",
        "map.batch_claimed",
        "map.refresh_started",
        "map.transaction_committed",
        "map.message_duplicate",
        "map.batch_acked",
        "map.degraded",
        "map.empty_checked",
        "map.runtime_destroyed",
    }
    records = [event.to_dict() for event in sink.events if event.action in expected]
    assert expected <= {record["action"] for record in records}
    for record in records:
        assert all(record.get(field) is not None for field in (
            "trace_id",
            "message_id",
            "project_id",
            "agent_id",
            "generation",
        ))
        rendered = str(record).lower()
        assert "source_code" not in rendered
        assert "diff" not in rendered
        assert "payload" not in rendered
