from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime import change_outbox as change_outbox_module
from bridle.agent.runtime.change_outbox import (
    AtomicPatchCommitter,
    ChangeCorrelation,
    ChangeIntent,
    ChangeOutbox,
    formal_write_entry_inventory,
)
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.mailbox import AgentAddress
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_registry import (
    ProjectRuntimeRegistry,
    configure_project_runtime_registry,
    reset_project_runtime_registry_for_tests,
)
from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent


class SimulatedCrash(BaseException):
    pass


@dataclass
class CapturingSink:
    events: list[LogEvent]

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


def _correlation(project_id: str = "project-outbox") -> ChangeCorrelation:
    return ChangeCorrelation(
        trace_id="trace-outbox",
        project_id=project_id,
        agent_id="session-agent",
        generation=3,
    )


def _outbox(
    root: Path,
    *,
    capacity: int = 100,
    failure_hook: Any | None = None,
    facade: LoggingFacade | None = None,
    busy_timeout_ms: int = 20,
    clock: Any | None = None,
) -> ChangeOutbox:
    kwargs: dict[str, Any] = {
        "project_id": "project-outbox",
        "capacity": capacity,
        "busy_timeout_ms": busy_timeout_ms,
        "failure_hook": failure_hook,
        "facade": facade,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return ChangeOutbox(root, **kwargs)


def _commit(
    outbox: ChangeOutbox,
    path: str = "src/value.py",
    *,
    change_type: str = "add",
    new_text: str | None = "value = 1\n",
) -> Any:
    return AtomicPatchCommitter(outbox).commit(
        path,
        change_type=change_type,  # type: ignore[arg-type]
        new_text=new_text,
        correlation=_correlation(),
    )


def _crash_at(stage: str):
    def hook(current: str, _intent: ChangeIntent) -> None:
        if current == stage:
            raise SimulatedCrash(stage)

    return hook


def _mailbox(root: Path, *, capacity: int = 100, busy_timeout_ms: int = 20) -> PersistentMailbox:
    return PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id="project-outbox",
        consumer_id="map-runtime",
        capacity=capacity,
        busy_timeout_ms=busy_timeout_ms,
    )


def test_reservation_backpressure_never_changes_target(test_workspace: Path) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    first = _outbox(test_workspace, capacity=1, failure_hook=_crash_at("after_reserved"))
    with pytest.raises(SimulatedCrash):
        _commit(first, change_type="modify", new_text="first\n")

    second = _outbox(test_workspace, capacity=1)
    result = _commit(second, change_type="modify", new_text="second\n")
    assert result.status == "backpressure"
    assert target.read_text(encoding="utf-8") == "before\n"

    connection = sqlite3.connect(second.database_path, timeout=0, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    try:
        busy = _outbox(test_workspace, capacity=2, busy_timeout_ms=0)
        result = _commit(busy, "src/other.py")
        assert result.status == "outbox_busy"
        assert not (test_workspace / "src" / "other.py").exists()
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize("stage", ["after_reserved", "after_staging_fsync"])
def test_recovery_abandons_reserved_and_releases_capacity(
    test_workspace: Path,
    stage: str,
) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    interrupted = _outbox(test_workspace, capacity=1, failure_hook=_crash_at(stage))
    with pytest.raises(SimulatedCrash):
        _commit(interrupted, change_type="modify", new_text="never-visible\n")

    reopened = _outbox(test_workspace, capacity=1)
    reopened.recover()
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not [item for item in reopened.intents() if item.state == "RESERVED"]
    assert _commit(reopened, change_type="modify", new_text="committed\n").status == "ready"


def test_atomic_create_modify_are_ready_before_success(test_workspace: Path) -> None:
    outbox = _outbox(test_workspace)
    added = _commit(outbox)
    assert added.status == "ready"
    assert added.intent is not None and added.intent.state == "READY"
    assert (test_workspace / "src" / "value.py").read_text(encoding="utf-8") == "value = 1\n"

    modified = _commit(outbox, change_type="modify", new_text="value = 2\n")
    assert modified.status == "ready"
    assert modified.intent is not None and modified.intent.message_id != added.intent.message_id
    assert (test_workspace / "src" / "value.py").read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.parametrize("stage", ["after_committing", "after_replace"])
def test_delete_tombstone_recovers_every_commit_window(test_workspace: Path, stage: str) -> None:
    target = test_workspace / "src" / "delete_me.py"
    target.parent.mkdir(parents=True)
    target.write_text("old content\n", encoding="utf-8")
    interrupted = _outbox(test_workspace, failure_hook=_crash_at(stage))
    with pytest.raises(SimulatedCrash):
        _commit(interrupted, "src/delete_me.py", change_type="remove", new_text=None)

    reopened = _outbox(test_workspace)
    recovered = reopened.recover()
    assert not target.exists()
    assert any(item.relative_path == "src/delete_me.py" and item.state == "READY" for item in recovered)
    first_ids = {item.message_id for item in reopened.intents() if item.relative_path == "src/delete_me.py"}
    reopened.recover()
    assert {item.message_id for item in reopened.intents() if item.relative_path == "src/delete_me.py"} == first_ids
    assert not list(target.parent.glob("*.bridle-tombstone-*"))


def test_delete_crash_after_ready_remains_ready_without_tombstone(test_workspace: Path) -> None:
    target = test_workspace / "src" / "delete_me.py"
    target.parent.mkdir(parents=True)
    target.write_text("old content\n", encoding="utf-8")
    interrupted = _outbox(test_workspace, failure_hook=_crash_at("after_ready"))
    with pytest.raises(SimulatedCrash):
        _commit(interrupted, "src/delete_me.py", change_type="remove", new_text=None)

    reopened = _outbox(test_workspace)
    reopened.recover()
    intent = reopened.intents()[0]
    assert intent.state == "READY"
    assert not target.exists()
    assert not list(target.parent.glob("*.bridle-tombstone-*"))


@pytest.mark.parametrize("stage", ["after_committing", "after_replace"])
def test_recover_committing_by_target_and_staging_digest(test_workspace: Path, stage: str) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    interrupted = _outbox(test_workspace, failure_hook=_crash_at(stage))
    with pytest.raises(SimulatedCrash):
        _commit(interrupted, change_type="modify", new_text="after\n")

    reopened = _outbox(test_workspace)
    recovered = reopened.recover()
    assert target.read_text(encoding="utf-8") == "after\n"
    assert len([item for item in recovered if item.state == "READY"]) == 1
    message_id = recovered[0].message_id
    reopened.recover()
    assert reopened.get(message_id) is not None
    assert reopened.get(message_id).state == "READY"  # type: ignore[union-attr]


def test_third_digest_requires_explicit_superseding_patch(test_workspace: Path) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    interrupted = _outbox(test_workspace, failure_hook=_crash_at("after_committing"))
    with pytest.raises(SimulatedCrash):
        _commit(interrupted, change_type="modify", new_text="outbox-after\n")
    target.write_text("external-third\n", encoding="utf-8")

    reopened = _outbox(test_workspace)
    recovered = reopened.recover()
    old = next(item for item in recovered if item.relative_path == "src/value.py")
    assert old.state == "REBASE_REQUIRED"
    assert target.read_text(encoding="utf-8") == "external-third\n"

    replacement = _commit(reopened, change_type="modify", new_text="explicit-new\n")
    assert replacement.status == "ready"
    assert replacement.intent is not None
    old = reopened.get(old.message_id)
    assert old is not None and old.superseded_by == replacement.intent.message_id
    assert target.read_text(encoding="utf-8") == "explicit-new\n"


def test_capacity_one_rebase_is_net_replaced_by_superseding_patch(test_workspace: Path) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    interrupted = _outbox(
        test_workspace,
        capacity=1,
        failure_hook=_crash_at("after_committing"),
    )
    with pytest.raises(SimulatedCrash):
        _commit(interrupted, change_type="modify", new_text="outbox-after\n")
    target.write_text("external-third\n", encoding="utf-8")
    reopened = _outbox(test_workspace, capacity=1)
    assert reopened.recover()[0].state == "REBASE_REQUIRED"

    replacement = _commit(reopened, change_type="modify", new_text="explicit-new\n")

    assert replacement.status == "ready"
    assert target.read_text(encoding="utf-8") == "explicit-new\n"
    assert reopened.publish_ready(_mailbox(test_workspace))[0].status == "delivered"
    assert _commit(reopened, "src/next.py").status == "ready"


def test_same_path_commits_and_recovery_are_fenced(test_workspace: Path) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()

    def hook(stage: str, _intent: ChangeIntent) -> None:
        if stage == "after_committing":
            entered.set()
            assert release.wait(2)

    first_result: list[Any] = []
    first = _outbox(test_workspace, failure_hook=hook)
    thread = threading.Thread(
        target=lambda: first_result.append(
            _commit(first, change_type="modify", new_text="first\n")
        ),
        daemon=True,
    )
    thread.start()
    assert entered.wait(2)
    competing = _commit(_outbox(test_workspace), change_type="modify", new_text="second\n")
    assert competing.status == "path_busy"
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert first_result[0].status == "ready"
    assert target.read_text(encoding="utf-8") == "first\n"
    active = [item for item in first.intents() if item.state in {"COMMITTING", "READY"}]
    assert len(active) == 1


@pytest.mark.parametrize(
    ("change_type", "new_text", "expected_text"),
    [
        ("modify", "writer-wins\n", "writer-wins\n"),
        ("remove", None, None),
    ],
)
def test_recovery_waits_for_active_writer_commit_boundary(
    test_workspace: Path,
    change_type: str,
    new_text: str | None,
    expected_text: str | None,
) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    recovery_done = threading.Event()

    def hook(stage: str, _intent: ChangeIntent) -> None:
        if stage == "after_committing":
            entered.set()
            assert release.wait(2)

    writer = threading.Thread(
        target=lambda: _commit(
            _outbox(test_workspace, failure_hook=hook),
            change_type=change_type,
            new_text=new_text,
        ),
        daemon=True,
    )
    writer.start()
    assert entered.wait(2)
    recovery = threading.Thread(
        target=lambda: (_outbox(test_workspace).recover(), recovery_done.set()),
        daemon=True,
    )
    recovery.start()
    assert not recovery_done.wait(0.1)
    release.set()
    writer.join(2)
    recovery.join(2)
    assert not writer.is_alive() and not recovery.is_alive()
    if expected_text is None:
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == expected_text
    assert [item.state for item in _outbox(test_workspace).intents()] == ["READY"]


def test_sync_fsync_failure_releases_reservation_and_never_changes_target(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(change_outbox_module.os, "fsync", fail_fsync)
    outbox = _outbox(test_workspace, capacity=1)
    result = _commit(outbox, change_type="modify", new_text="never-visible\n")
    assert result.status == "failed"
    assert target.read_text(encoding="utf-8") == "before\n"
    assert outbox.intents() == []


def test_ready_publish_is_idempotent_and_retries_full_busy_mail(test_workspace: Path) -> None:
    now = [datetime(2026, 7, 15, tzinfo=UTC)]
    outbox = _outbox(test_workspace, clock=lambda: now[0])
    ready = _commit(outbox)
    assert ready.intent is not None
    blocked_mail = _mailbox(test_workspace, capacity=1)
    filler = AtomicPatchCommitter(_outbox(test_workspace / "filler", capacity=1))
    del filler  # Capacity is exercised by a real envelope below, not by a fake mailbox result.
    from bridle.agent.runtime.mailbox import MailEnvelope

    blocked_mail.enqueue(
        MailEnvelope(
            message_id="mail-filler",
            message_type="TaskAssigned",
            source=AgentAddress("project-outbox", "source", 1),
            target=AgentAddress("project-outbox", "target", 1),
            payload={},
        )
    )
    retry = outbox.publish_ready(blocked_mail)
    assert retry[0].status == "publish_retry"
    assert outbox.get(ready.intent.message_id).state == "READY"  # type: ignore[union-attr]

    healthy_root = test_workspace / "healthy-mail"
    healthy_mail = _mailbox(healthy_root)
    now[0] += timedelta(seconds=2)
    delivered = outbox.publish_ready(healthy_mail)
    assert delivered[0].status == "delivered"
    assert outbox.get(ready.intent.message_id).state == "DELIVERED"  # type: ignore[union-attr]
    assert outbox.publish_ready(healthy_mail) == []


@dataclass
class MutableClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def test_publish_retry_is_persisted_and_waits_until_due(test_workspace: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 15, tzinfo=UTC))
    outbox = _outbox(test_workspace, clock=clock)
    ready = _commit(outbox)
    assert ready.intent is not None
    mailbox = _mailbox(test_workspace, capacity=1)
    from bridle.agent.runtime.mailbox import MailEnvelope

    mailbox.enqueue(
        MailEnvelope(
            message_id="mail-filler",
            message_type="TaskAssigned",
            source=AgentAddress("project-outbox", "source", 1),
            target=AgentAddress("project-outbox", "target", 1),
            payload={},
        )
    )
    first = outbox.publish_ready(mailbox)
    assert first[0].status == "publish_retry"
    persisted = outbox.get(ready.intent.message_id)
    assert persisted is not None and persisted.next_retry_at is not None
    assert outbox.publish_ready(mailbox) == []
    clock.advance(2)
    assert outbox.publish_ready(mailbox)[0].status == "publish_retry"


def test_publish_retry_saturates_before_large_attempt_overflow(test_workspace: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 15, tzinfo=UTC))
    outbox = _outbox(test_workspace, clock=clock)
    ready = _commit(outbox)
    assert ready.intent is not None
    with closing(sqlite3.connect(outbox.database_path)) as connection:
        connection.execute(
            "UPDATE change_intents SET attempt = 2000 WHERE message_id = ?",
            (ready.intent.message_id,),
        )
        connection.commit()
    mailbox = _mailbox(test_workspace, capacity=1)
    from bridle.agent.runtime.mailbox import MailEnvelope

    mailbox.enqueue(
        MailEnvelope(
            message_id="mail-filler",
            message_type="TaskAssigned",
            source=AgentAddress("project-outbox", "source", 1),
            target=AgentAddress("project-outbox", "target", 1),
            payload={},
        )
    )
    result = outbox.publish_ready(mailbox)
    assert result[0].status == "publish_retry"
    persisted = outbox.get(ready.intent.message_id)
    assert persisted is not None and persisted.attempt == 2001
    assert persisted.next_retry_at == "2026-07-15T00:01:00.000000Z"


def test_mail_enqueued_crash_replays_existing_id_then_delivers(test_workspace: Path) -> None:
    crashed = False

    def hook(stage: str, _intent: ChangeIntent) -> None:
        nonlocal crashed
        if stage == "after_mail_enqueue" and not crashed:
            crashed = True
            raise SimulatedCrash(stage)

    outbox = _outbox(test_workspace, failure_hook=hook)
    ready = _commit(outbox)
    assert ready.intent is not None
    mailbox = _mailbox(test_workspace)
    with pytest.raises(SimulatedCrash):
        outbox.publish_ready(mailbox)
    assert outbox.get(ready.intent.message_id).state == "READY"  # type: ignore[union-attr]
    assert outbox.publish_ready(mailbox)[0].status == "delivered"
    assert outbox.get(ready.intent.message_id).state == "DELIVERED"  # type: ignore[union-attr]


def test_real_sqlite_mail_busy_schedules_retry(test_workspace: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 15, tzinfo=UTC))
    outbox = _outbox(test_workspace, clock=clock)
    ready = _commit(outbox)
    assert ready.intent is not None
    mailbox = _mailbox(test_workspace, busy_timeout_ms=0)
    connection = sqlite3.connect(mailbox.database_path, timeout=0, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = outbox.publish_ready(mailbox)
    finally:
        connection.rollback()
        connection.close()
    assert result[0].status == "publish_retry"
    assert result[0].error_code == "mailbox_busy"
    clock.advance(2)
    assert outbox.publish_ready(mailbox)[0].status == "delivered"


@pytest.mark.asyncio
async def test_gateway_forwarder_publishes_ready_and_stops_cleanly(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    from bridle.agent.runtime import gateway as gateway_module

    await gateway_module.shutdown_gateway_runtimes()
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    configure_project_runtime_registry(
        ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    )
    ready = _commit(_outbox(test_workspace))
    assert ready.intent is not None
    try:
        handle = await gateway_module._ensure_change_outbox_forwarder(
            project_path=str(test_workspace),
            project_id="project-outbox",
            facade=LoggingFacade(sinks=[]),
            trace_id="trace-outbox",
        )
        deadline = asyncio.get_running_loop().time() + 2
        receipt_exists = False
        mail_status = None
        while asyncio.get_running_loop().time() < deadline:
            plan_db = test_workspace / ".bridle" / "plan.db"
            if plan_db.exists():
                with closing(sqlite3.connect(plan_db)) as connection:
                    receipt_exists = connection.execute(
                        "SELECT 1 FROM map_applied_messages WHERE message_id = ?",
                        (ready.intent.message_id,),
                    ).fetchone() is not None
            with closing(sqlite3.connect(handle.mailbox.database_path)) as connection:
                row = connection.execute(
                    "SELECT status FROM mail_messages WHERE message_id = ?",
                    (ready.intent.message_id,),
                ).fetchone()
                mail_status = None if row is None else str(row[0])
            if receipt_exists and mail_status == "delivered":
                break
            await asyncio.sleep(0.01)
        assert receipt_exists
        assert mail_status == "delivered"
        gateway_source = inspect.getsource(gateway_module)
        assert "await _ensure_change_outbox_forwarder(" in gateway_source
    finally:
        await gateway_module.shutdown_gateway_runtimes()
        reset_project_runtime_registry_for_tests()
    assert handle.task.done()


@pytest.mark.asyncio
async def test_forwarder_recovers_from_iteration_error_and_logs_retry(
    test_workspace: Path,
) -> None:
    recovered = asyncio.Event()
    calls = 0

    def hook(stage: str, _intent: ChangeIntent) -> None:
        nonlocal calls
        if stage != "after_mail_enqueue":
            return
        calls += 1
        if calls == 1:
            raise RuntimeError("injected forwarder iteration failure")
        recovered.set()

    sink = CapturingSink([])
    facade = LoggingFacade(sinks=[sink])
    outbox = _outbox(test_workspace, failure_hook=hook, facade=facade)
    ready = _commit(outbox)
    assert ready.intent is not None
    mailbox = _mailbox(test_workspace)
    forwarder = change_outbox_module.ChangeOutboxForwarder(
        outbox,
        mailbox,
        poll_seconds=0.01,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(forwarder.run(stop))
    await asyncio.wait_for(recovered.wait(), timeout=1)
    stop.set()
    await task
    assert outbox.get(ready.intent.message_id).state == "DELIVERED"  # type: ignore[union-attr]
    failure = next(
        event for event in sink.events if event.action == "change_outbox.forwarder_error"
    )
    failure_record = failure.to_dict()
    assert failure_record["error_code"] == "RuntimeError"
    assert failure_record["trace_id"] == ready.intent.correlation.trace_id
    assert failure_record["message_id"] == ready.intent.message_id
    assert failure_record["project_id"] == ready.intent.correlation.project_id
    assert failure_record["agent_id"] == ready.intent.correlation.agent_id
    assert failure_record["generation"] == ready.intent.correlation.generation
    assert "injected forwarder iteration failure" not in str(failure_record)


@pytest.mark.asyncio
async def test_forwarder_retries_wake_after_mail_is_already_delivered(
    test_workspace: Path,
) -> None:
    outbox = _outbox(test_workspace)
    ready = _commit(outbox)
    assert ready.intent is not None
    mailbox = _mailbox(test_workspace)
    woke = asyncio.Event()
    attempts = 0

    async def wake(_intent: ChangeIntent) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected wake failure")
        woke.set()

    forwarder = change_outbox_module.ChangeOutboxForwarder(
        outbox,
        mailbox,
        poll_seconds=0.01,
        wake_callback=wake,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(forwarder.run(stop))
    try:
        await asyncio.wait_for(woke.wait(), timeout=1)
    finally:
        stop.set()
        await task
    assert attempts == 2
    assert outbox.get(ready.intent.message_id).state == "DELIVERED"  # type: ignore[union-attr]
    with closing(sqlite3.connect(mailbox.database_path)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM mail_messages WHERE message_id = ?",
            (ready.intent.message_id,),
        ).fetchone()
    assert count == (1,)
    await mailbox.close()


@pytest.mark.asyncio
async def test_gateway_shutdown_closes_mailbox_after_forwarder_task_failure(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime import gateway as gateway_module

    await gateway_module.shutdown_gateway_runtimes()
    mailbox = _mailbox(test_workspace)

    async def fail() -> None:
        raise RuntimeError("injected stopped task")

    task = asyncio.create_task(fail())
    with pytest.raises(RuntimeError):
        await task
    key = (str(test_workspace.resolve()), "project-outbox")
    gateway_module._outbox_forwarders[key] = gateway_module._OutboxForwarderHandle(
        stop=asyncio.Event(),
        task=task,
        mailbox=mailbox,
    )
    await gateway_module.shutdown_gateway_runtimes()
    target = AgentAddress("project-outbox", "map-runtime", 1)
    assert (await mailbox.receive(target, timeout=0)).status == "closed"


def test_existing_outbox_schema_migrates_next_retry_column(test_workspace: Path) -> None:
    database_path = test_workspace / ".bridle" / "change_outbox.db"
    database_path.parent.mkdir(parents=True)
    old_schema = change_outbox_module._SCHEMA.replace("    next_retry_at TEXT,\n", "")
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(old_schema)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('project_id', 'project-outbox')"
        )
        connection.commit()
    _outbox(test_workspace)
    with closing(sqlite3.connect(database_path)) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(change_intents)").fetchall()
        }
    assert "next_retry_at" in columns


def test_formal_write_entry_inventory_is_bound_to_actual_executor_source() -> None:
    inventory = formal_write_entry_inventory()
    source = inspect.getsource(change_outbox_module.AtomicPatchCommitter.commit_many)
    assert inventory["formal_entries"] == ["AtomicPatchCommitter.commit_many"]
    assert inventory["required_committer"] == "AtomicPatchCommitter"
    assert inventory["direct_mutation_methods"] == ["commit_many"]
    assert "Publish one candidate change set" in source
    assert "os.replace" in source
    assert inventory["source_sha256"] == change_outbox_module.hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()


def test_change_outbox_logs_full_correlation_without_content(test_workspace: Path) -> None:
    sink = CapturingSink([])
    outbox = _outbox(test_workspace, facade=LoggingFacade(sinks=[sink]))
    result = _commit(outbox, new_text="secret-source-body\n")
    assert result.status == "ready"
    actions = {event.action for event in sink.events}
    assert {
        "change_outbox.reserved",
        "change_outbox.committing",
        "change_outbox.write_committed",
        "change_outbox.ready",
    } <= actions
    for event in sink.events:
        payload = event.to_dict()
        assert payload["trace_id"] == "trace-outbox"
        assert payload["message_id"]
        assert payload["project_id"] == "project-outbox"
        assert payload["agent_id"] == "session-agent"
        assert payload["generation"] == 3
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "secret-source-body" not in serialized
        assert "staging" not in serialized.lower()


def test_all_outbox_lifecycle_logs_have_correlation_duration_and_retry_attempt(
    test_workspace: Path,
) -> None:
    sink = CapturingSink([])
    facade = LoggingFacade(sinks=[sink])
    target = test_workspace / "src" / "value.py"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    interrupted = _outbox(
        test_workspace,
        failure_hook=_crash_at("after_committing"),
        facade=facade,
    )
    with pytest.raises(SimulatedCrash):
        _commit(interrupted, change_type="modify", new_text="uncommitted-secret\n")
    target.write_text("external-third\n", encoding="utf-8")
    clock = MutableClock(datetime(2026, 7, 15, tzinfo=UTC))
    outbox = _outbox(test_workspace, facade=facade, clock=clock)
    outbox.recover()
    replacement = _commit(outbox, change_type="modify", new_text="replacement-secret\n")
    assert replacement.status == "ready"
    blocked = _mailbox(test_workspace, capacity=1)
    from bridle.agent.runtime.mailbox import MailEnvelope

    blocked.enqueue(
        MailEnvelope(
            message_id="filler",
            message_type="TaskAssigned",
            source=AgentAddress("project-outbox", "source", 1),
            target=AgentAddress("project-outbox", "target", 1),
            payload={},
        )
    )
    assert outbox.publish_ready(blocked)[0].status == "publish_retry"
    clock.advance(2)
    assert outbox.publish_ready(_mailbox(test_workspace / "healthy"))[0].status == "delivered"

    expected = {
        "change_outbox.reserved",
        "change_outbox.committing",
        "change_outbox.write_committed",
        "change_outbox.ready",
        "change_outbox.publish_retry",
        "change_outbox.delivered",
        "change_outbox.recovered",
        "change_outbox.rebase_required",
        "change_outbox.superseded",
    }
    relevant = [event for event in sink.events if event.action in expected]
    assert {event.action for event in relevant} == expected
    for event in relevant:
        payload = event.to_dict()
        assert payload["trace_id"] == "trace-outbox"
        assert payload["message_id"]
        assert payload["project_id"] == "project-outbox"
        assert payload["agent_id"] == "session-agent"
        assert payload["generation"] == 3
        assert isinstance(payload["duration_ms"], int)
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "uncommitted-secret" not in serialized
        assert "replacement-secret" not in serialized
        assert "staging" not in serialized.lower()
    retry_event = next(
        event for event in relevant if event.action == "change_outbox.publish_retry"
    )
    assert retry_event.to_dict()["detail"]["attempt"] == 1
