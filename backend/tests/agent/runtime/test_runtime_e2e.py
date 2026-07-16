from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import bridle.logging.facade as logging_facade_module
from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
from bridle.agent.runtime.change_outbox import ChangeOutbox
from bridle.agent.runtime.mailbox import AgentAddress
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_map_agent import ProjectMapAgent, ProjectMapAgentState
from bridle.agent.runtime.project_registry import (
    ProjectRuntimeRegistry,
    configure_project_runtime_registry,
)
from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor
from bridle.features.project_map.store import ProjectPlanStore
from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent
from bridle.models.agent_runtime import AgentRuntimeRecord
from tests.agent.runtime.test_agent_runtime_host import _grant


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


def _mailbox(root: Path, project_id: str, consumer_id: str) -> PersistentMailbox:
    return PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id=consumer_id,
        lease_seconds=1,
        retry_base_seconds=0.01,
        retry_max_seconds=0.01,
        default_target=AgentAddress(project_id, "map-runtime", 1),
    )


def _mail_status(mailbox: PersistentMailbox, message_id: str) -> str:
    with closing(sqlite3.connect(mailbox.database_path)) as connection:
        row = connection.execute(
            "SELECT status FROM mail_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


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


@pytest.mark.asyncio
async def test_failed_formal_patch_does_not_change_outbox(test_workspace: Path) -> None:
    policy = SandboxPolicy.for_run(
        run_id="run-ar07-failed",
        node_id="node-ar07-failed",
        workspace_root=test_workspace,
        allowed_files=[".bridle/blocked.py"],
        node_tests=[],
    )
    executor = SandboxedToolExecutor(
        policy,
        project_id="project-ar07",
        agent_id="agent-ar07",
        generation=7,
        trace_id="trace-ar07",
        formal_workspace=True,
    )
    executor.tdd_state.bypass_for_test_setup()
    outbox = ChangeOutbox(test_workspace, project_id="project-ar07")
    before = [
        (item.relative_path, item.message_id, item.correlation.generation)
        for item in outbox.intents()
    ]

    result = await executor.propose_file_patch(
        ".bridle/blocked.py",
        "--- /dev/null\n+++ b/.bridle/blocked.py\n@@ -0,0 +1 @@\n+blocked = True\n",
        "add",
    )

    after = [
        (item.relative_path, item.message_id, item.correlation.generation)
        for item in ChangeOutbox(test_workspace, project_id="project-ar07").intents()
    ]
    assert result["status"] == "failed"
    assert after == before


@pytest.mark.asyncio
async def test_formal_patch_redelivery_after_map_commit_is_idempotent_and_retires(
    test_workspace: Path,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "project-ar07-e2e"
    agent_id = "parent-ar07-e2e"
    trace_id = "trace-ar07-e2e"
    generation = 1
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.initialize(scan_if_created=False)
    before_seq = store.latest_change_seq()
    sink = _CaptureSink()
    monkeypatch.setattr(
        logging_facade_module,
        "_global_facade",
        LoggingFacade(sinks=[sink]),
    )
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    from bridle.agent.runtime import gateway

    host, _coordinator = await gateway._components_for(db)
    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id=project_id,
        session_id="session-ar07-e2e",
        agent_id=agent_id,
        generation=generation,
        grant=_grant(project_id),
    )
    await host.transition(parent, RuntimeState.RUNNING, reason="formal_patch_started")
    policy = SandboxPolicy.for_run(
        run_id="run-ar07-e2e",
        node_id="node-ar07-e2e",
        workspace_root=test_workspace,
        allowed_files=["src/formal.py"],
        node_tests=[],
    )
    executor = SandboxedToolExecutor(
        policy,
        project_id=project_id,
        agent_id=agent_id,
        generation=generation,
        trace_id=trace_id,
        formal_workspace=True,
    )
    executor.tdd_state.bypass_for_test_setup()

    patch_result = await executor.propose_file_patch(
        "src/formal.py",
        "--- /dev/null\n+++ b/src/formal.py\n@@ -0,0 +1 @@\n+formal = True\n",
        "add",
    )
    outbox = ChangeOutbox(test_workspace, project_id=project_id)
    intents = outbox.intents()
    assert patch_result["status"] == "completed"
    assert len(intents) == 1
    intent = intents[0]
    assert intent.state == "READY"

    def crash_after_commit() -> None:
        raise RuntimeError("crash_after_commit")

    def map_agent_factory(*args, **kwargs):
        if kwargs["generation"] == 1:
            kwargs["after_commit_hook"] = crash_after_commit
        return ProjectMapAgent(*args, **kwargs)

    registry = ProjectRuntimeRegistry(
        runtime_host=host,
        agent_factory=map_agent_factory,
    )
    configure_project_runtime_registry(registry)
    await gateway._ensure_change_outbox_forwarder(
        project_path=str(test_workspace),
        project_id=project_id,
        facade=LoggingFacade(sinks=[sink]),
        trace_id=trace_id,
    )
    await _wait_until(
        lambda: registry.generation(project_id) == 1
        and registry.active_count == 1
        and registry.get(project_id).state is ProjectMapAgentState.FAILED
        and registry.get(project_id).task is not None
        and registry.get(project_id).task.done()
    )
    first_agent = registry.get(project_id)
    assert first_agent.task is not None
    with pytest.raises(RuntimeError, match="crash_after_commit"):
        await first_agent.task
    committed_seq = store.latest_change_seq()
    assert committed_seq > before_seq
    assert _receipt_ids(store) == {intent.message_id}
    first_mailbox = _mailbox(test_workspace, project_id, "assert-first-failure")
    assert _mail_status(first_mailbox, intent.message_id) == "pending"
    await first_mailbox.close()

    await gateway.recover_project_runtime(
        project_path=str(test_workspace),
        project_id=project_id,
        facade=LoggingFacade(sinks=[sink]),
    )
    delivered_mailbox = _mailbox(test_workspace, project_id, "assert-delivered")
    await _wait_until(
        lambda: _mail_status(delivered_mailbox, intent.message_id) == "delivered"
    )
    await delivered_mailbox.close()
    await _wait_until(lambda: registry.active_count == 0)
    assert store.latest_change_seq() == committed_seq
    assert _receipt_ids(store) == {intent.message_id}
    shutdown_result = await gateway.shutdown_gateway_runtimes()

    assert shutdown_result.failures == ()
    assert parent.state is RuntimeState.DESTROYED
    assert host.active_handles() == ()
    async with sessions() as session:
        records = (await session.execute(select(AgentRuntimeRecord))).scalars().all()
    assert {record.status for record in records} == {"DESTROYED"}
    correlated = [event for event in sink.events if event.message_id == intent.message_id]
    assert correlated
    assert all(event.project_id == project_id for event in correlated)
    assert all(event.trace_id for event in correlated)
    assert all(event.agent_id for event in correlated)
    assert all(event.generation is not None for event in correlated)
    assert any(event.trace_id == trace_id for event in correlated)
