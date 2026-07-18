from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bridle.database as database_module
import bridle.logging.facade as logging_facade_module
from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.container.container_service import configure_runner
from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
from bridle.agent.runtime.change_outbox import (
    AtomicPatchCommitter,
    ChangeCorrelation,
    ChangeOutbox,
)
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.input_relay import RuntimeInputRelay
from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationState,
    ModificationWorkflow,
)
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.project_map_agent import ProjectRuntimeShutdownError
from bridle.agent.runtime.project_registry import (
    ProjectRuntimeRegistry,
    configure_project_runtime_registry,
)
from bridle.agent.runtime.session_runtime_lifecycle import RuntimeSessionLifecycle
from bridle.app import create_app
from bridle.database import configure_sqlite_engine
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.projects.service import ProjectService
from bridle.features.sessions.service import ProjectSessionService
from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent
from bridle.models.agent_runtime import AgentRuntimeRecord, RuntimeInputDeliveryRecord
from bridle.models.project import ProjectRecord
from bridle.models.project_message import ProjectMessageRecord
from bridle.models.project_runtime_recovery import ProjectRuntimeRecoveryRecord
from bridle.models.project_session import ProjectSessionRecord
from tests.agent.runtime.test_agent_runtime_host import _database, _grant
from tests.helpers.verification_fixtures import (
    PassingStructuredRunner,
    advance_to_implementing,
    freeze_contract_for_candidate_identity,
)


@pytest_asyncio.fixture
async def runtime_db(tmp_path: Path):
    engine, sessions = await _database(tmp_path)
    try:
        yield engine, sessions
    finally:
        await engine.dispose()


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []
        self.forced = asyncio.Event()

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)
        if event.action == "app.runtime_shutdown_forced":
            self.forced.set()


def _prepare_implementing_candidate(
    store: ProjectPlanStore,
    node_id: str,
    candidate_id: str,
):
    safe_name = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:6]
    source_path = f"src/{safe_name}.py"
    test_path = f"tests/test_{safe_name}.py"
    source = store.project_root / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file = store.project_root / test_path
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        f"def test_{safe_name}():\n    assert True\n",
        encoding="utf-8",
    )
    command = f"python -m pytest {test_path} -q"
    snapshot = {
        "module_id": f"m-{safe_name}",
        "node_id": node_id,
        "implementation_entities": [{"entity_id": f"impl-{node_id}", "path": source_path}],
        "test_entities": [{"entity_id": f"test-{node_id}", "path": test_path}],
        "test_commands": [command],
        "interfaces": [],
        "test_dir": "tests",
    }
    setup = CandidateExecutionService(store.project_root).prepare_from_snapshot(
        snapshot,
        run_id=f"setup-{node_id}",
        candidate_id=candidate_id,
        base_map_seq=1,
    )
    workflow = ModificationWorkflow(store)
    contract = freeze_contract_for_candidate_identity(
        workflow,
        node_id,
        project_root=store.project_root,
        test_commands=[command],
        test_paths=[test_path],
        map_seq=setup.request.base_map_seq,
        boundary_fingerprint=setup.request.boundary_fingerprint,
        image_version=setup.request.image_version,
    )
    advance_to_implementing(workflow, node_id, contract)
    return setup, contract, workflow


def _persist_final_run(
    store: ProjectPlanStore,
    workflow: ModificationWorkflow,
    *,
    node_id: str,
    candidate_id: str,
    contract_version: str,
    run_id: str,
) -> dict:
    submitted = workflow.apply(
        node_id,
        event=ModificationEvent.SUBMITTED,
        event_id=f"setup:{node_id}:submitted",
        payload={"candidate_id": candidate_id},
    )
    run = store.enqueue_verification_run(
        run_id=run_id,
        node_id=node_id,
        phase="final",
        source_revision=int(submitted["revision"]),
        contract_version=contract_version,
        candidate_id=candidate_id,
    )
    workflow.apply(
        node_id,
        event=ModificationEvent.FINAL_VERIFICATION_STARTED,
        event_id=f"verification:{run_id}:started",
        payload={"verification_run_id": run_id, "phase": "final"},
    )
    return run


async def _wait_for_modification_state(
    workflow: ModificationWorkflow,
    node_id: str,
    state: ModificationState,
    *,
    timeout: float = 2.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while workflow.get(node_id)["state"] != state.value:
        if loop.time() >= deadline:
            pytest.fail(
                f"modification state did not reach {state.value}: "
                f"{workflow.get(node_id)['state']}"
            )
        await asyncio.sleep(0.01)


def _receipt_exists(root: Path, message_id: str) -> bool:
    with closing(sqlite3.connect(root / ".bridle" / "plan.db")) as connection:
        return connection.execute(
            "SELECT 1 FROM map_applied_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone() is not None


async def _enqueue(
    root: Path,
    project_id: str,
    message_id: str,
    *,
    payload: dict[str, object] | None = None,
) -> None:
    mailbox = PersistentMailbox(
        root / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id="ar07-producer",
    )
    result = mailbox.enqueue(
        MailEnvelope(
            message_id=message_id,
            message_type="CodeChanged",
            source=AgentAddress(project_id, "change-outbox", 1),
            target=AgentAddress(project_id, "map-runtime", 1),
            payload=payload or {"path": "a.py"},
        )
    )
    assert result.status == "inserted"
    await mailbox.close()


@pytest.mark.asyncio
async def test_lifespan_interrupts_every_legacy_role_state_and_relays_input_without_parent_restart(
    tmp_path: Path,
    runtime_db,
) -> None:
    project_id = "legacy-matrix-project"
    session_id = "legacy-matrix-session"
    _engine, sessions = runtime_db
    target = AgentAddress(project_id, "parent", 1)
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id="legacy-matrix-relay",
        default_target=target,
    )
    async with sessions() as db:
        db.add(ProjectRecord(id=project_id, path=str(tmp_path), name="legacy-matrix"))
        db.add(
            ProjectSessionRecord(
                id=session_id,
                project_id=project_id,
                project_path_snapshot=str(tmp_path),
                title="legacy matrix",
                role="planning",
                status="active",
            )
        )
        for role in ("parent", "child", "map"):
            for index, state in enumerate(("CREATING", "READY", "RUNNING", "STOPPING"), 1):
                db.add(
                    AgentRuntimeRecord(
                        id=f"{role}-{state.lower()}",
                        runtime_type=role,
                        owner_type="session" if role != "map" else "project",
                        owner_id=session_id if role != "map" else project_id,
                        project_id=project_id,
                        session_id=session_id if role != "map" else None,
                        agent_id=f"{role}-{state.lower()}",
                        generation=index,
                        status=state,
                    )
                )
        await db.commit()
        pending = await ProjectSessionService.create_runtime_input(
            db,
            session_id,
            content="recover me",
            target=target,
        )
    host = AgentRuntimeHost(sessions)
    relay = RuntimeInputRelay(sessions, mailbox_for_project=lambda _project_id: mailbox)
    lifecycle = RuntimeSessionLifecycle(sessions, relay=relay, host=host)

    assert await lifecycle.recover_before_requests() == 13

    async with sessions() as db:
        rows = (await db.execute(select(AgentRuntimeRecord))).scalars().all()
        delivery = (
            await db.execute(
                select(RuntimeInputDeliveryRecord).where(
                    RuntimeInputDeliveryRecord.message_id == pending.id
                )
            )
        ).scalar_one()
    assert len(rows) == 12
    assert {row.status for row in rows} == {"INTERRUPTED"}
    assert {row.status_reason for row in rows} == {"process_restart"}
    assert delivery.status == "delivered"
    assert host.active_handles() == ()
    await mailbox.close()


@pytest.mark.asyncio
async def test_recovery_status_table_upgrades_legacy_database_idempotently(
    tmp_path: Path,
) -> None:
    import bridle.models  # noqa: F401
    from bridle.models.base import Base

    engine = configure_sqlite_engine(
        create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'application.db').as_posix()}",
            echo=False,
        )
    )
    legacy_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name != "project_runtime_recovery"
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=legacy_tables)
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(ProjectRecord(id="legacy-project", path=str(tmp_path), name="legacy"))
        db.add(
            ProjectSessionRecord(
                id="legacy-session",
                project_id="legacy-project",
                project_path_snapshot=str(tmp_path),
                title="legacy session",
                role="planning",
                status="closed",
            )
        )
        db.add(
            AgentRuntimeRecord(
                id="legacy-runtime",
                runtime_type="parent",
                owner_type="session",
                owner_id="legacy-session",
                project_id="legacy-project",
                session_id="legacy-session",
                agent_id="legacy-parent",
                generation=1,
                status="INTERRUPTED",
            )
        )
        await db.commit()

    async def legacy_snapshot() -> dict[str, list[dict]]:
        snapshot: dict[str, list[dict]] = {}
        async with sessions() as db:
            for model in (ProjectRecord, ProjectSessionRecord, AgentRuntimeRecord):
                rows = (
                    await db.execute(select(model.__table__).order_by(model.__table__.c.id))
                ).mappings().all()
                snapshot[model.__tablename__] = [dict(row) for row in rows]
        return snapshot

    before = await legacy_snapshot()
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=registry,
    )

    try:
        async with engine.begin() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
        assert "project_runtime_recovery" not in tables

        for _attempt in range(2):
            async with app.router.lifespan_context(app):
                pass
            assert await legacy_snapshot() == before

        async with engine.begin() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
        assert "project_runtime_recovery" in tables
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lifespan_recovers_pending_map_mail_before_requests(
    tmp_path: Path,
    runtime_db,
) -> None:
    project_id = "project-recovery"
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    ProjectPlanStore(tmp_path, project_id=project_id).initialize(scan_if_created=False)
    await _enqueue(tmp_path, project_id, "recovery-message")
    _engine, sessions = runtime_db
    async with sessions() as db:
        db.add(ProjectRecord(id=project_id, path=str(tmp_path), name="recovery"))
        await db.commit()
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=registry,
    )

    async with app.router.lifespan_context(app):
        assert _receipt_exists(tmp_path, "recovery-message")
        assert registry.generation(project_id) == 1

    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_lifespan_recovers_verification_without_new_request(
    tmp_path: Path,
    runtime_db,
) -> None:
    project_id = "project-verification-recovery"
    store = ProjectPlanStore(tmp_path, project_id=project_id)
    store.initialize(scan_if_created=False)
    fixtures = {}
    for label, short_id in (
        ("queued", "q"),
        ("deferred", "d"),
        ("expired-running", "r"),
    ):
        node_id = f"node-verification-{label}"
        candidate_id = f"c-{short_id}"
        setup, contract, workflow = _prepare_implementing_candidate(
            store,
            node_id,
            candidate_id,
        )
        run = _persist_final_run(
            store,
            workflow,
            node_id=node_id,
            candidate_id=candidate_id,
            contract_version=contract.contract_version,
            run_id=f"verify-{label}",
        )
        fixtures[label] = {
            "node_id": node_id,
            "candidate_id": candidate_id,
            "candidate_root": setup.workspace.root,
            "run_id": run["run_id"],
        }

    now = time.time()
    deferred_claim = store.claim_verification_run(
        str(fixtures["deferred"]["run_id"]),
        now_timestamp=now,
        lease_seconds=60,
    )
    assert deferred_claim is not None
    deferred = store.defer_verification_run(
        str(fixtures["deferred"]["run_id"]),
        lease_token=str(deferred_claim["lease_token"]),
        error_code="container_temporarily_unavailable",
        now_timestamp=now,
    )
    future_deadline = float(deferred["next_retry_at"])
    assert future_deadline > time.time()
    expired = store.claim_verification_run(
        str(fixtures["expired-running"]["run_id"]),
        now_timestamp=now - 120,
        lease_seconds=1,
    )
    assert expired is not None
    assert float(expired["lease_expires_at"]) < time.time()

    runner = PassingStructuredRunner(tmp_path)
    configure_runner(tmp_path, runner)
    del workflow, store, setup, contract, run
    from bridle.agent.runtime import gateway
    _engine, sessions = runtime_db
    async with sessions() as db:
        db.add(ProjectRecord(id=project_id, path=str(tmp_path), name="verification"))
        await db.commit()
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=registry,
    )

    async with app.router.lifespan_context(app):
        reopened = ProjectPlanStore.open_existing(tmp_path)
        reopened_workflow = ModificationWorkflow(reopened)
        for fixture in fixtures.values():
            await _wait_for_modification_state(
                reopened_workflow,
                str(fixture["node_id"]),
                ModificationState.READY_TO_PUBLISH,
                timeout=4.0,
            )
            result_path = Path(fixture["candidate_root"]) / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            assert result["candidate_id"] == fixture["candidate_id"]
            assert result["status"] == "ready"
        assert {item["candidate_id"] for item in runner.executions} == {
            fixture["candidate_id"] for fixture in fixtures.values()
        }
        deferred_execution = next(
            item
            for item in runner.executions
            if item["candidate_id"] == fixtures["deferred"]["candidate_id"]
        )
        assert float(deferred_execution["executed_at"]) >= future_deadline

    assert gateway._verification_loops == {}
    assert not any(
        task.get_name().startswith("verification-loop:") and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_running_verification_loop_discovers_persisted_trigger_without_new_request(
    tmp_path: Path,
    runtime_db,
) -> None:
    project_id = "project-verification-running-loop"
    store = ProjectPlanStore(tmp_path, project_id=project_id)
    store.initialize(scan_if_created=False)
    node_id = "node-verification-running-loop"
    candidate_id = "c-loop"
    setup, _contract, workflow = _prepare_implementing_candidate(
        store,
        node_id,
        candidate_id,
    )
    runner = PassingStructuredRunner(tmp_path)
    configure_runner(tmp_path, runner)
    candidate_root = setup.workspace.root
    del workflow, store, setup
    _engine, sessions = runtime_db
    async with sessions() as db:
        db.add(ProjectRecord(id=project_id, path=str(tmp_path), name="verification-loop"))
        await db.commit()
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=registry,
    )

    async with app.router.lifespan_context(app):
        independent_store = ProjectPlanStore.open_existing(tmp_path)
        independent_workflow = ModificationWorkflow(independent_store)
        independent_workflow.apply(
            node_id,
            event=ModificationEvent.SUBMITTED,
            event_id=f"independent:{node_id}:submitted",
            payload={"candidate_id": candidate_id},
        )
        await _wait_for_modification_state(
            independent_workflow,
            node_id,
            ModificationState.READY_TO_PUBLISH,
        )
        result = json.loads((candidate_root / "result.json").read_text(encoding="utf-8"))
        assert result["candidate_id"] == candidate_id
        assert result["status"] == "ready"
        assert len(runner.executions) == 1


@pytest.mark.asyncio
async def test_lifespan_recovers_ready_outbox_pending_mail_and_skips_empty_projects(
    tmp_path: Path,
    runtime_db,
) -> None:
    ready_root = tmp_path / "ready"
    pending_root = tmp_path / "pending"
    empty_root = tmp_path / "empty"
    ready_root.mkdir()
    pending_root.mkdir()
    empty_root.mkdir()
    ProjectPlanStore(ready_root, project_id="ready-project").initialize(
        scan_if_created=False
    )
    ProjectPlanStore(empty_root, project_id="empty-project").initialize(
        scan_if_created=False
    )
    ProjectPlanStore(pending_root, project_id="pending-project").initialize(
        scan_if_created=False
    )
    await _enqueue(pending_root, "pending-project", "pending-message")
    intent = AtomicPatchCommitter(
        ChangeOutbox(ready_root, project_id="ready-project")
    ).commit(
        "a.py",
        change_type="add",
        new_text="A = 1\n",
        correlation=ChangeCorrelation("trace-ready", "ready-project", "parent", 1),
    ).intent
    assert intent is not None
    empty_mailbox = PersistentMailbox(
        empty_root / ".bridle" / "mail.db",
        project_id="empty-project",
        consumer_id="empty-init",
    )
    await empty_mailbox.close()
    _engine, sessions = runtime_db
    async with sessions() as db:
        db.add_all(
            [
                ProjectRecord(id="ready-project", path=str(ready_root), name="ready"),
                ProjectRecord(
                    id="pending-project", path=str(pending_root), name="pending"
                ),
                ProjectRecord(id="empty-project", path=str(empty_root), name="empty"),
            ]
        )
        await db.commit()
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=registry,
    )

    async with app.router.lifespan_context(app):
        assert _receipt_exists(ready_root, intent.message_id)
        assert _receipt_exists(pending_root, "pending-message")
        assert registry.generation("ready-project") == 1
        assert registry.generation("pending-project") == 1
        assert registry.generation("empty-project") == 0


@pytest.mark.asyncio
async def test_lifespan_persists_and_exposes_each_project_recovery_failure(
    tmp_path: Path,
    runtime_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_root = tmp_path / "healthy"
    corrupt_plan_root = tmp_path / "corrupt-plan"
    corrupt_mail_root = tmp_path / "corrupt-mail"
    corrupt_outbox_root = tmp_path / "corrupt-outbox"
    unavailable_root = tmp_path / "unavailable"
    healthy_root.mkdir()
    corrupt_plan_root.mkdir()
    corrupt_mail_root.mkdir()
    corrupt_outbox_root.mkdir()
    (healthy_root / "a.py").write_text("A = 1\n", encoding="utf-8")
    ProjectPlanStore(healthy_root, project_id="healthy-project").initialize(
        scan_if_created=False
    )
    await _enqueue(healthy_root, "healthy-project", "healthy-message")
    ProjectPlanStore(corrupt_plan_root, project_id="corrupt-plan-project").initialize(
        scan_if_created=False
    )
    (corrupt_plan_root / ".bridle" / "plan.db").write_bytes(b"not-sqlite")
    for root, project_id in (
        (corrupt_mail_root, "corrupt-mail-project"),
        (corrupt_outbox_root, "corrupt-outbox-project"),
    ):
        ProjectPlanStore(root, project_id=project_id).initialize(scan_if_created=False)
        ChangeOutbox(root, project_id=project_id)
        mailbox = PersistentMailbox(
            root / ".bridle" / "mail.db",
            project_id=project_id,
            consumer_id="corrupt-init",
        )
        await mailbox.close()
    (corrupt_mail_root / ".bridle" / "mail.db").write_bytes(b"not-sqlite")
    (corrupt_outbox_root / ".bridle" / "change_outbox.db").write_bytes(b"not-sqlite")
    _engine, sessions = runtime_db
    async with sessions() as db:
        db.add_all(
            [
                ProjectRecord(
                    id="healthy-project", path=str(healthy_root), name="healthy"
                ),
                ProjectRecord(id="corrupt-plan-project", path=str(corrupt_plan_root), name="plan"),
                ProjectRecord(id="corrupt-mail-project", path=str(corrupt_mail_root), name="mail"),
                ProjectRecord(id="corrupt-outbox-project", path=str(corrupt_outbox_root), name="outbox"),
                ProjectRecord(
                    id="unavailable-project",
                    path=str(unavailable_root),
                    name="unavailable",
                ),
            ]
        )
        await db.commit()
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    sink = _CaptureSink()
    monkeypatch.setattr(
        logging_facade_module,
        "_global_facade",
        LoggingFacade(sinks=[sink]),
    )
    configure_project_runtime_registry(registry)
    monkeypatch.setattr(database_module, "_ensure_engine", lambda: None)
    monkeypatch.setattr(database_module, "async_session", sessions)
    app = create_app()

    async with app.router.lifespan_context(app):
        assert not unavailable_root.exists()
        assert _receipt_exists(healthy_root, "healthy-message")
        async with sessions() as db:
            degraded = (
                await db.execute(select(ProjectRuntimeRecoveryRecord))
            ).scalars().all()
            assert {row.project_id for row in degraded} == {
                "corrupt-plan-project",
                "corrupt-mail-project",
                "corrupt-outbox-project",
                "unavailable-project",
            }
            projects = await ProjectService.list_projects(db)
        by_id = {project.id: project for project in projects}
        assert by_id["healthy-project"].scan_status != "stale"
        assert by_id["corrupt-plan-project"].scan_status == "stale"
        assert by_id["corrupt-mail-project"].scan_status == "stale"
        assert by_id["corrupt-outbox-project"].scan_status == "stale"
        assert by_id["unavailable-project"].scan_status == "stale"
        assert not by_id["corrupt-plan-project"].can_chat
        degraded_events = [
            event for event in sink.events if event.action == "app.runtime_project_degraded"
        ]
        assert {event.project_id for event in degraded_events} == {
            "corrupt-plan-project",
            "corrupt-mail-project",
            "corrupt-outbox-project",
            "unavailable-project",
        }
        for event in degraded_events:
            assert event.detail == {"reason": "project_recovery_failed"}


@pytest.mark.asyncio
async def test_shutdown_latch_remains_closed_after_repeated_stop(
    tmp_path: Path,
    runtime_db,
) -> None:
    _engine, sessions = runtime_db
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))

    await registry.begin_shutdown()
    await registry.stop_all()
    await registry.stop_all()

    with pytest.raises(ProjectRuntimeShutdownError) as error:
        await registry.wake("late-project", tmp_path)
    assert error.value.error_code == "project_runtime_registry_shutting_down"


@pytest.mark.asyncio
async def test_shutdown_continues_after_one_gateway_destroy_fails(
    tmp_path: Path,
    runtime_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bridle.agent.runtime import gateway

    _engine, sessions = runtime_db
    async with sessions() as db:
        host, _coordinator = await gateway._components_for(db)
    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="shutdown-failure-project",
        session_id="shutdown-failure-session",
        agent_id="shutdown-parent",
        generation=1,
        grant=_grant("shutdown-failure-project"),
    )
    child = await host.create_runtime(
        role=RuntimeRole.CHILD,
        project_id="shutdown-failure-project",
        session_id="shutdown-failure-session",
        agent_id="shutdown-child",
        generation=1,
        grant=_grant("shutdown-failure-project"),
        parent=parent,
    )
    await host.transition(parent, RuntimeState.RUNNING, reason="test_started")
    await host.transition(child, RuntimeState.RUNNING, reason="test_started")
    original_destroy = host.destroy
    failed_once = False

    async def fail_first_destroy(handle):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("first_destroy_failed")
        return await original_destroy(handle)

    monkeypatch.setattr(host, "destroy", fail_first_destroy)
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    configure_project_runtime_registry(registry)

    result = await gateway.shutdown_gateway_runtimes()

    assert len(result.failures) == 1
    assert {handle.state for handle in (parent, child)} == {RuntimeState.DESTROYED}
    assert host.active_handles() == ()
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_shutdown_retries_failed_map_destroy_on_registry_host(
    tmp_path: Path,
    runtime_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProjectPlanStore(tmp_path, project_id="map-destroy-retry").initialize(
        scan_if_created=False
    )
    _engine, sessions = runtime_db
    host = AgentRuntimeHost(sessions)
    registry = ProjectRuntimeRegistry(runtime_host=host)
    original_destroy = host.destroy
    failed_once = False

    async def fail_first_destroy(handle):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("first_map_destroy_failed")
        return await original_destroy(handle)

    monkeypatch.setattr(host, "destroy", fail_first_destroy)
    await registry.wake("map-destroy-retry", tmp_path)

    result = await registry.stop_all()

    assert failed_once
    assert len(result.failures) == 1
    assert result.failures[0].error_code == "project_runtime_stop_failed"
    assert registry.active_count == 0
    assert host.active_handles() == ()


@pytest.mark.asyncio
async def test_shutdown_reports_completed_map_finalizer_failure_and_preserves_non_map(
    tmp_path: Path,
    runtime_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "map-finalizer-retry"
    ProjectPlanStore(tmp_path, project_id=project_id).initialize(scan_if_created=False)
    _engine, sessions = runtime_db
    sink = _CaptureSink()
    facade = LoggingFacade(sinks=[sink])
    host = AgentRuntimeHost(sessions, facade=facade)
    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id=project_id,
        session_id="map-finalizer-session",
        agent_id="map-finalizer-parent",
        generation=1,
        grant=_grant(project_id),
    )
    await host.transition(parent, RuntimeState.RUNNING, reason="test_started")
    registry = ProjectRuntimeRegistry(runtime_host=host, logging_facade=facade)
    original_destroy = host.destroy
    failed = asyncio.Event()

    async def fail_first_map_destroy(handle):
        if handle.spec.role is RuntimeRole.MAP and not failed.is_set():
            failed.set()
            raise RuntimeError("retired_map_destroy_failed")
        return await original_destroy(handle)

    monkeypatch.setattr(host, "destroy", fail_first_map_destroy)
    await registry.wake(project_id, tmp_path)
    await asyncio.wait_for(failed.wait(), timeout=1)
    await asyncio.sleep(0)

    result = await registry.stop_all()

    assert len(result.failures) == 1
    assert result.failures[0].project_id == project_id
    assert result.failures[0].error_code == "project_runtime_finalizer_failed"
    assert result.failures[0].error_type == "RuntimeError"
    assert registry.active_count == 0
    assert host.active_handles() == (parent,)
    assert parent.state is RuntimeState.RUNNING
    assert any(event.action == "map.runtime_destroy_failed" for event in sink.events)
    await original_destroy(parent)


@pytest.mark.asyncio
async def test_shutdown_latches_before_forwarder_join_and_awaits_all_runtime_finalizers(
    tmp_path: Path,
    runtime_db,
) -> None:
    project_id = "project-finalizer"
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    ProjectPlanStore(tmp_path, project_id=project_id).initialize(scan_if_created=False)
    await _enqueue(tmp_path, project_id, "finalizer-message")
    _engine, sessions = runtime_db
    from bridle.agent.runtime import gateway

    async with sessions() as db:
        host, _coordinator = await gateway._components_for(db)
    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id=project_id,
        session_id="finalizer-session",
        agent_id="finalizer-parent",
        generation=1,
        grant=_grant(project_id),
    )
    child = await host.create_runtime(
        role=RuntimeRole.CHILD,
        project_id=project_id,
        session_id="finalizer-session",
        agent_id="finalizer-child",
        generation=1,
        grant=_grant(project_id),
        parent=parent,
    )
    await host.transition(parent, RuntimeState.RUNNING, reason="test_started")
    await host.transition(child, RuntimeState.RUNNING, reason="test_started")
    forwarder = await gateway._ensure_change_outbox_forwarder(
        project_path=str(tmp_path),
        project_id=project_id,
        facade=LoggingFacade(sinks=[]),
        trace_id="finalizer-trace",
    )
    removed = asyncio.Event()
    destroy_started = asyncio.Event()
    destroy_release = asyncio.Event()
    original_destroy = host.destroy

    async def blocked_destroy(handle):
        destroy_started.set()
        await destroy_release.wait()
        return await original_destroy(handle)

    host.destroy = blocked_destroy  # type: ignore[method-assign]

    async def retire_hook(_project_id: str, stage: str) -> None:
        if stage == "after_removal":
            removed.set()

    registry = ProjectRuntimeRegistry(runtime_host=host, retire_hook=retire_hook)
    agent = await registry.wake(project_id, tmp_path)
    try:
        await asyncio.wait_for(removed.wait(), timeout=1)
        await asyncio.wait_for(destroy_started.wait(), timeout=1)
        app = create_app(
            test_workspace=str(tmp_path),
            runtime_lifecycle=RuntimeSessionLifecycle(sessions),
            project_runtime_registry=registry,
        )

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                pass

        shutdown = asyncio.create_task(run_lifespan())
        await asyncio.sleep(0)
        assert not shutdown.done()
        destroy_release.set()
        await asyncio.wait_for(shutdown, timeout=1)
        assert agent.runtime_handle is not None
        assert agent.runtime_handle.state.value == "DESTROYED"
        assert registry.active_count == 0
        assert host.active_handles() == ()
        assert parent.state is RuntimeState.DESTROYED
        assert child.state is RuntimeState.DESTROYED
        assert forwarder.task.done()
        with pytest.raises(ProjectRuntimeShutdownError):
            await registry.wake("late-project", tmp_path)
    finally:
        destroy_release.set()
        await registry.stop_all()


@pytest.mark.asyncio
async def test_shutdown_timeout_finishes_real_runtime_cleanup_and_logs_forced(
    tmp_path: Path,
    runtime_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "project-forced-shutdown"
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    ProjectPlanStore(tmp_path, project_id=project_id).initialize(scan_if_created=False)
    await _enqueue(tmp_path, project_id, "forced-message")
    _engine, sessions = runtime_db
    from bridle.agent.runtime import gateway

    async with sessions() as db:
        host, _coordinator = await gateway._components_for(db)
    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id=project_id,
        session_id="forced-session",
        agent_id="forced-parent",
        generation=1,
        grant=_grant(project_id),
    )
    child = await host.create_runtime(
        role=RuntimeRole.CHILD,
        project_id=project_id,
        session_id="forced-session",
        agent_id="forced-child",
        generation=1,
        grant=_grant(project_id),
        parent=parent,
    )
    await host.transition(parent, RuntimeState.RUNNING, reason="test_started")
    await host.transition(child, RuntimeState.RUNNING, reason="test_started")
    forwarder = await gateway._ensure_change_outbox_forwarder(
        project_path=str(tmp_path),
        project_id=project_id,
        facade=LoggingFacade(sinks=[]),
        trace_id="forced-trace",
    )
    destroy_started = asyncio.Event()
    destroy_release = asyncio.Event()
    original_destroy = host.destroy

    async def blocked_destroy(handle):
        destroy_started.set()
        await destroy_release.wait()
        return await original_destroy(handle)

    host.destroy = blocked_destroy  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(runtime_host=host)
    agent = await registry.wake(project_id, tmp_path)
    await asyncio.wait_for(destroy_started.wait(), timeout=1)
    sink = _CaptureSink()
    monkeypatch.setattr(
        logging_facade_module,
        "_global_facade",
        LoggingFacade(sinks=[sink]),
    )
    app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=registry,
        runtime_shutdown_timeout=0.01,
    )

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    shutdown = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(sink.forced.wait(), timeout=1)
        assert not shutdown.done()
        destroy_release.set()
        await asyncio.wait_for(shutdown, timeout=1)
    finally:
        destroy_release.set()
        if not shutdown.done():
            await shutdown

    assert agent.runtime_handle is not None
    assert agent.runtime_handle.state.value == "DESTROYED"
    assert registry.active_count == 0
    assert host.active_handles() == ()
    assert parent.state is RuntimeState.DESTROYED
    assert child.state is RuntimeState.DESTROYED
    assert forwarder.task.done()
    assert [
        event.action
        for event in sink.events
        if event.action.startswith("app.runtime_shutdown_")
    ] == [
        "app.runtime_shutdown_started",
        "app.runtime_shutdown_forced",
        "app.runtime_shutdown_completed",
    ]
    with pytest.raises(ProjectRuntimeShutdownError):
        await registry.wake("late-project", tmp_path)
    with closing(sqlite3.connect(tmp_path / ".bridle" / "mail.db")) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mail_messages WHERE status = 'leased'"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_preserves_persisted_history(
    tmp_path: Path,
    runtime_db,
) -> None:
    project_id = "project-history"
    store = ProjectPlanStore(tmp_path, project_id=project_id)
    store.initialize(scan_if_created=False)
    outbox = ChangeOutbox(tmp_path, project_id=project_id)
    intent = AtomicPatchCommitter(outbox).commit(
        "history.py",
        change_type="add",
        new_text="history = True\n",
        correlation=ChangeCorrelation("history-trace", project_id, "history-parent", 1),
    ).intent
    assert intent is not None
    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute(
            "INSERT INTO map_applied_messages(message_id) VALUES (?)",
            (intent.message_id,),
        )
        connection.commit()
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id="history",
    )
    assert outbox.publish_ready(mailbox)[0].status == "delivered"
    target = AgentAddress(project_id, "map-runtime", 1)
    claimed = mailbox.claim(target)
    assert claimed.status == "claimed"
    assert mailbox.ack(
        intent.message_id,
        claimed.lease_token,
        target=target,
    ).status == "acked"
    await mailbox.close()
    _engine, sessions = runtime_db
    async with sessions() as db:
        db.add(ProjectRecord(id=project_id, path=str(tmp_path), name="history"))
        db.add(
            ProjectSessionRecord(
                id="history-session",
                project_id=project_id,
                project_path_snapshot=str(tmp_path),
                title="history-title",
                role="planning",
                status="closed",
            )
        )
        db.add(
            ProjectMessageRecord(
                id="history-message",
                session_id="history-session",
                role="user",
                content="memory-sentinel",
                tool_calls=[{"name": "remember", "arguments": {"key": "value"}}],
                tool_result={"remembered": True},
            )
        )
        db.add(
            AgentRuntimeRecord(
                id="history-runtime",
                runtime_type="parent",
                owner_type="project",
                owner_id=project_id,
                project_id=project_id,
                agent_id="history-parent",
                generation=1,
                status="INTERRUPTED",
            )
        )
        await db.commit()

    async def application_snapshot() -> dict[str, list[dict]]:
        snapshot: dict[str, list[dict]] = {}
        async with sessions() as db:
            for model in (
                ProjectSessionRecord,
                ProjectMessageRecord,
                AgentRuntimeRecord,
            ):
                rows = (
                    await db.execute(select(model.__table__).order_by(model.__table__.c.id))
                ).mappings().all()
                snapshot[model.__tablename__] = [dict(row) for row in rows]
        return snapshot

    def project_snapshot() -> dict[str, list[tuple]]:
        snapshot: dict[str, list[tuple]] = {}
        for database_name, table_name in (
            ("plan.db", "map_applied_messages"),
            ("change_outbox.db", "change_intents"),
            ("mail.db", "mail_messages"),
        ):
            with closing(
                sqlite3.connect(tmp_path / ".bridle" / database_name)
            ) as connection:
                snapshot[table_name] = connection.execute(
                    f"SELECT * FROM {table_name} ORDER BY 1"
                ).fetchall()
        return snapshot

    original_application = await application_snapshot()
    original_project = project_snapshot()
    first_registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    first_app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=first_registry,
    )
    async with first_app.router.lifespan_context(first_app):
        pass
    await first_registry.stop_all()
    first_application = await application_snapshot()
    first_project = project_snapshot()
    assert first_application == original_application
    assert first_project == original_project

    second_registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    second_app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=second_registry,
    )
    async with second_app.router.lifespan_context(second_app):
        pass
    await second_registry.stop_all()
    await second_registry.stop_all()

    assert await application_snapshot() == first_application
    assert project_snapshot() == first_project
    assert first_application["project_messages"][0]["content"] == "memory-sentinel"
    assert first_project["change_intents"]
    assert first_project["mail_messages"]
    assert first_project["map_applied_messages"]


@pytest.mark.asyncio
async def test_runtime_lifecycle_logs_are_correlated_and_redacted(
    tmp_path: Path,
    runtime_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_root = tmp_path / "log-healthy"
    corrupt_root = tmp_path / "log-corrupt-secret-sentinel"
    healthy_root.mkdir()
    corrupt_root.mkdir()
    ProjectPlanStore(healthy_root, project_id="log-healthy").initialize(
        scan_if_created=False
    )
    (healthy_root / "a.py").write_text("A = 1\n", encoding="utf-8")
    await _enqueue(
        healthy_root,
        "log-healthy",
        "log-correlation-message",
        payload={
            "path": "a.py",
            "metadata": {
                "source": "nested-source-sentinel",
                "diff": "nested-diff-sentinel",
                "prompt": "nested-prompt-sentinel",
            },
        },
    )
    ProjectPlanStore(corrupt_root, project_id="log-corrupt").initialize(
        scan_if_created=False
    )
    ChangeOutbox(healthy_root, project_id="log-healthy")
    healthy_mail = PersistentMailbox(
        healthy_root / ".bridle" / "mail.db",
        project_id="log-healthy",
        consumer_id="log-init",
    )
    await healthy_mail.close()
    (corrupt_root / ".bridle" / "plan.db").write_bytes(b"not-sqlite")
    _engine, sessions = runtime_db
    async with sessions() as db:
        db.add_all(
            [
                ProjectRecord(id="log-healthy", path=str(healthy_root), name="healthy"),
                ProjectRecord(id="log-corrupt", path=str(corrupt_root), name="corrupt"),
            ]
        )
        await db.commit()
    sink = _CaptureSink()
    monkeypatch.setattr(
        logging_facade_module,
        "_global_facade",
        LoggingFacade(sinks=[sink]),
    )
    registry = ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))
    app = create_app(
        test_workspace=str(tmp_path),
        runtime_lifecycle=RuntimeSessionLifecycle(sessions),
        project_runtime_registry=registry,
        runtime_shutdown_timeout=0,
    )

    async with app.router.lifespan_context(app):
        pass

    actions = {event.action for event in sink.events}
    assert {
        "app.runtime_recovery_started",
        "app.runtime_recovery_completed",
        "app.runtime_project_recovered",
        "app.runtime_project_degraded",
        "app.runtime_shutdown_started",
        "app.runtime_shutdown_forced",
        "app.runtime_shutdown_completed",
    } <= actions
    project_events = [
        event
        for event in sink.events
        if event.action in {
            "app.runtime_project_recovered",
            "app.runtime_project_degraded",
        }
    ]
    assert {event.project_id for event in project_events} == {
        "log-healthy",
        "log-corrupt",
    }
    forbidden = {"path", "payload", "source", "diff", "prompt"}

    def assert_detail_redacted(value: object) -> None:
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            for nested in value.values():
                assert_detail_redacted(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                assert_detail_redacted(nested)
        elif isinstance(value, str):
            assert "nested-source-sentinel" not in value
            assert "nested-diff-sentinel" not in value
            assert "nested-prompt-sentinel" not in value

    for event in sink.events:
        assert_detail_redacted(event.detail)
    serialized = json.dumps(
        [asdict(event) for event in sink.events],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "log-corrupt-secret-sentinel" not in serialized
    assert "nested-source-sentinel" not in serialized
    assert "nested-diff-sentinel" not in serialized
    assert "nested-prompt-sentinel" not in serialized
    for forbidden_value in ("source =", "diff --git", "full prompt"):
        assert forbidden_value not in serialized
    boundary_events = [event for event in sink.events if event.message_id is not None]
    assert boundary_events
    for event in boundary_events:
        assert event.trace_id
        assert event.project_id
        assert event.agent_id
        assert event.generation is not None
