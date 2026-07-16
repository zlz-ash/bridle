from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bridle.database import configure_sqlite_engine
from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent
from bridle.observability.context import bind_log_context, reset_log_context


class CapturingSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


class FailingSink:
    def emit(self, event: LogEvent) -> None:
        raise RuntimeError("secret database payload")


def _create_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", echo=False)
    return configure_sqlite_engine(engine)


async def _create_application_schema(path: Path) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    import bridle.models  # noqa: F401
    from bridle.models.base import Base

    engine = _create_engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _columns(path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _unique_column_sets(path: Path, table: str) -> set[frozenset[str]]:
    with closing(sqlite3.connect(path)) as connection:
        unique_indexes = [row for row in connection.execute(f"PRAGMA index_list({table})") if row[2]]
        return {
            frozenset(str(column[2]) for column in connection.execute(f"PRAGMA index_info({row[1]})"))
            for row in unique_indexes
        }


def _metadata(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            str(key): str(value)
            for key, value in connection.execute("SELECT key, value FROM metadata").fetchall()
        }


def _sqlite_snapshot(path: Path, *, data_tables: tuple[str, ...]) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            "schema": connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall(),
            "metadata": connection.execute(
                "SELECT key, value FROM metadata ORDER BY key"
            ).fetchall(),
            "data": {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in data_tables
            },
        }


@pytest.mark.asyncio
async def test_application_database_creates_runtime_tables_and_preserves_existing_tables(
    tmp_path: Path,
) -> None:
    import bridle.models as models

    assert models.AgentRuntimeRecord.__tablename__ == "agent_runtimes"
    assert models.RuntimeInputDeliveryRecord.__tablename__ == "runtime_input_deliveries"
    database_path = tmp_path / "application.db"
    engine, _ = await _create_application_schema(database_path)
    await engine.dispose()

    assert {
        "projects",
        "project_sessions",
        "project_messages",
        "agent_runtimes",
        "runtime_input_deliveries",
    } <= _tables(database_path)
    assert {
        "id",
        "runtime_type",
        "owner_type",
        "owner_id",
        "project_id",
        "session_id",
        "parent_agent_id",
        "agent_id",
        "generation",
        "status",
        "result_summary",
        "error_summary",
        "created_at",
        "updated_at",
    } <= _columns(database_path, "agent_runtimes")
    assert {
        "id",
        "message_id",
        "session_message_id",
        "project_id",
        "session_id",
        "target_address",
        "target_agent_id",
        "target_generation",
        "status",
        "attempt",
        "mail_enqueued_at",
        "created_at",
        "updated_at",
    } <= _columns(database_path, "runtime_input_deliveries")
    assert frozenset({"message_id"}) in _unique_column_sets(
        database_path, "runtime_input_deliveries"
    )
    assert {"id", "path", "name", "last_opened_at"} <= _columns(database_path, "projects")


@pytest.mark.asyncio
async def test_application_database_runtime_record_round_trips_across_reconnect(
    tmp_path: Path,
) -> None:
    from bridle.agent.runtime.persistence import add_runtime_record, get_runtime_record

    database_path = tmp_path / "runtime-roundtrip.db"
    engine, sessions = await _create_application_schema(database_path)
    async with sessions() as session:
        complete = await add_runtime_record(
            session,
            runtime_type="parent",
            owner_type="session",
            owner_id="session-1",
            project_id="project-1",
            session_id="session-1",
            parent_agent_id=None,
            agent_id="agent-1",
            generation=3,
            status="RUNNING",
            result_summary="safe result",
            error_summary="safe error",
        )
        nullable = await add_runtime_record(
            session,
            runtime_type="map",
            owner_type="project",
            owner_id="project-2",
            project_id=None,
            session_id=None,
            parent_agent_id=None,
            agent_id="map-agent",
            generation=1,
            status="READY",
        )
        await session.commit()
        complete_id = complete.id
        nullable_id = nullable.id
    await engine.dispose()

    reconnected_engine = _create_engine(database_path)
    reconnected_sessions = async_sessionmaker(
        reconnected_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with reconnected_sessions() as session:
        complete = await get_runtime_record(session, complete_id)
        nullable = await get_runtime_record(session, nullable_id)

    assert complete.runtime_type == "parent"
    assert complete.owner_id == "session-1"
    assert complete.project_id == "project-1"
    assert complete.session_id == "session-1"
    assert complete.agent_id == "agent-1"
    assert complete.generation == 3
    assert complete.status == "RUNNING"
    assert complete.result_summary == "safe result"
    assert complete.error_summary == "safe error"
    assert complete.created_at is not None and complete.updated_at is not None
    assert nullable.project_id is None
    assert nullable.session_id is None
    assert nullable.parent_agent_id is None
    assert nullable.result_summary is None
    assert nullable.error_summary is None
    await reconnected_engine.dispose()


@pytest.mark.asyncio
async def test_application_database_delivery_commits_and_rolls_back_atomically(
    tmp_path: Path,
) -> None:
    from bridle.agent.runtime.persistence import add_runtime_input_delivery
    from bridle.models import (
        ProjectMessageRecord,
        ProjectRecord,
        ProjectSessionRecord,
        RuntimeInputDeliveryRecord,
    )

    database_path = tmp_path / "delivery-transaction.db"
    engine, sessions = await _create_application_schema(database_path)

    async with sessions() as session:
        project = ProjectRecord(path=str(tmp_path / "project"), name="project")
        project_session = ProjectSessionRecord(
            project=project,
            project_path_snapshot=project.path,
            title="session",
            role="planning",
            status="active",
        )
        message = ProjectMessageRecord(session=project_session, role="user", content="hello")
        session.add_all([project, project_session, message])
        await session.flush()
        await add_runtime_input_delivery(
            session,
            message_id="message-1",
            session_message_id=message.id,
            project_id=project.id,
            session_id=project_session.id,
            target_address="agent://project-1/agent-1/1",
            target_agent_id="agent-1",
            target_generation=1,
        )
        await session.commit()

    async with sessions() as session:
        message_count = await session.scalar(select(func.count()).select_from(ProjectMessageRecord))
        delivery_count = await session.scalar(
            select(func.count()).select_from(RuntimeInputDeliveryRecord)
        )
        assert message_count == 1
        assert delivery_count == 1

        rolled_back_message = ProjectMessageRecord(
            session_id=project_session.id,
            role="user",
            content="rollback",
        )
        session.add(rolled_back_message)
        await session.flush()
        await add_runtime_input_delivery(
            session,
            message_id="message-rollback",
            session_message_id=rolled_back_message.id,
            project_id=project.id,
            session_id=project_session.id,
            target_address="agent://project-1/agent-1/1",
            target_agent_id="agent-1",
            target_generation=1,
        )
        await session.rollback()

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectMessageRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(RuntimeInputDeliveryRecord)) == 1
        with pytest.raises(IntegrityError):
            await add_runtime_input_delivery(
                session,
                message_id="message-1",
                session_message_id=message.id,
                project_id=project.id,
                session_id=project_session.id,
                target_address="agent://project-1/agent-2/1",
                target_agent_id="agent-2",
                target_generation=1,
            )
        await session.rollback()

    delivery_columns = _columns(database_path, "runtime_input_deliveries")
    assert {
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "acked_at",
        "nacked_at",
    }.isdisjoint(delivery_columns)
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_database_runtime_state_change_is_persistent_and_logged(
    tmp_path: Path,
    caplog,
) -> None:
    import bridle.agent.runtime.persistence as persistence

    captured = CapturingSink()
    facade = LoggingFacade(sinks=[FailingSink(), captured])
    database_path = tmp_path / "runtime-state.db"
    engine, sessions = await _create_application_schema(database_path)

    bind_log_context(trace_id="trace-runtime", message_id="message-runtime")
    try:
        with caplog.at_level("ERROR", logger="bridle.logging"):
            async with sessions() as session:
                record = await persistence.add_runtime_record(
                    session,
                    runtime_type="child",
                    owner_type="parent",
                    owner_id="parent-1",
                    project_id="project-1",
                    session_id="session-1",
                    parent_agent_id="parent-1",
                    agent_id="child-1",
                    generation=2,
                    status="READY",
                    facade=facade,
                )
                await session.commit()
                runtime_id = record.id
            async with sessions() as session:
                await persistence.update_runtime_state(
                    session,
                    runtime_id,
                    status="FAILED",
                    error_summary="safe summary",
                    facade=facade,
                )
                await session.commit()
    finally:
        reset_log_context()
    await engine.dispose()

    reconnected_engine = _create_engine(database_path)
    reconnected_sessions = async_sessionmaker(
        reconnected_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with reconnected_sessions() as session:
        persisted = await persistence.get_runtime_record(session, runtime_id)
    await reconnected_engine.dispose()

    assert persisted.status == "FAILED"
    assert persisted.error_summary == "safe summary"
    actions = [event.action for event in captured.events]
    assert actions == ["runtime_record.created", "runtime_record.state_changed"]
    for event in captured.events:
        payload = event.to_dict()
        assert payload["trace_id"] == "trace-runtime"
        assert payload["message_id"] == "message-runtime"
        assert payload["project_id"] == "project-1"
        assert payload["agent_id"] == "child-1"
        assert payload["generation"] == 2
    assert "secret database payload" not in caplog.text
    assert not hasattr(persistence, "interrupt_stale_runtimes")
    assert not hasattr(persistence, "recover_runtime_records")


def test_project_storage_creates_isolated_store_schemas(test_workspace: Path) -> None:
    from bridle.agent.runtime.project_storage import initialize_project_storage

    project_root = (test_workspace / "project").resolve()
    project_root.mkdir()
    (project_root / "main.py").write_text("value = 1\n", encoding="utf-8")
    captured = CapturingSink()
    facade = LoggingFacade(sinks=[captured])
    bind_log_context(trace_id="trace-storage")
    try:
        paths = initialize_project_storage(
            project_root,
            project_id="project-storage",
            facade=facade,
        )
    finally:
        reset_log_context()

    assert project_root.drive.upper() == "D:"
    databases = {paths.mail_db.resolve(), paths.change_outbox_db.resolve(), paths.plan_db.resolve()}
    assert len(databases) == 3
    assert all(path.drive.upper() == "D:" and path.is_relative_to(project_root) for path in databases)
    assert "mail_messages" in _tables(paths.mail_db)
    assert "change_outbox_entries" not in _tables(paths.mail_db)
    assert "change_outbox_entries" in _tables(paths.change_outbox_db)
    assert "mail_messages" not in _tables(paths.change_outbox_db)
    assert "map_applied_messages" in _tables(paths.plan_db)
    assert {"mail_messages", "change_outbox_entries"}.isdisjoint(_tables(paths.plan_db))
    assert _metadata(paths.mail_db)["store_kind"] == "mail"
    assert _metadata(paths.change_outbox_db)["store_kind"] == "change_outbox"
    assert _metadata(paths.plan_db)["store_kind"] == "plan"

    assert {
        "message_id",
        "source_address",
        "target_address",
        "message_type",
        "payload_json",
        "sequence_no",
        "status",
        "attempt",
        "next_retry_at",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "created_at",
        "updated_at",
    } <= _columns(paths.mail_db, "mail_messages")
    assert frozenset({"message_id"}) in _unique_column_sets(paths.mail_db, "mail_messages")
    assert {
        "message_id",
        "relative_path",
        "before_sha256",
        "after_sha256",
        "staging_path",
        "status",
        "attempt",
        "next_retry_at",
        "created_at",
        "updated_at",
    } <= _columns(paths.change_outbox_db, "change_outbox_entries")
    assert frozenset({"message_id"}) in _unique_column_sets(
        paths.change_outbox_db, "change_outbox_entries"
    )
    assert {"message_id", "applied_at"} <= _columns(paths.plan_db, "map_applied_messages")
    assert frozenset({"message_id"}) in _unique_column_sets(
        paths.plan_db, "map_applied_messages"
    )

    initialization_events = [
        event for event in captured.events if event.action == "project_db.initialized"
    ]
    assert {event.detail["store_kind"] for event in initialization_events} == {
        "mail",
        "change_outbox",
        "plan",
    }
    for event in initialization_events:
        payload = event.to_dict()
        assert payload["project_id"] == "project-storage"
        assert payload["trace_id"] == "trace-storage"
        assert payload["duration_ms"] >= 0
        serialized = json.dumps(payload, ensure_ascii=False)
        assert str(project_root) not in serialized
        assert "payload_json" not in serialized


def test_project_storage_reinitialization_preserves_rows(test_workspace: Path) -> None:
    from bridle.agent.runtime.project_storage import initialize_project_storage

    project_root = test_workspace / "reinitialize"
    project_root.mkdir()
    paths = initialize_project_storage(project_root, project_id="project-reinitialize")
    with closing(sqlite3.connect(paths.mail_db)) as connection:
        connection.execute(
            "INSERT INTO mail_messages(message_id, source_address, target_address, message_type, "
            "payload_json, sequence_no, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "mail-1",
                "agent://project-reinitialize/source/1",
                "agent://project-reinitialize/target/1",
                "CodeChanged",
                "{}",
                1,
                "2026-07-14T00:00:00.000000Z",
                "2026-07-14T00:00:00.000000Z",
            ),
        )
        connection.commit()
    with closing(sqlite3.connect(paths.change_outbox_db)) as connection:
        connection.execute(
            "INSERT INTO change_outbox_entries(message_id, relative_path, before_sha256, "
            "after_sha256, staging_path) VALUES (?, ?, ?, ?, ?)",
            ("outbox-1", "main.py", "before", "after", ".bridle/staging/1"),
        )
        connection.commit()
    with closing(sqlite3.connect(paths.plan_db)) as connection:
        connection.execute(
            "INSERT INTO map_applied_messages(message_id) VALUES (?)",
            ("map-1",),
        )
        connection.commit()

    initialize_project_storage(project_root, project_id="project-reinitialize")

    with closing(sqlite3.connect(paths.mail_db)) as connection:
        assert connection.execute("SELECT message_id FROM mail_messages").fetchall() == [("mail-1",)]
    with closing(sqlite3.connect(paths.change_outbox_db)) as connection:
        assert connection.execute("SELECT message_id FROM change_outbox_entries").fetchall() == [
            ("outbox-1",)
        ]
    with closing(sqlite3.connect(paths.plan_db)) as connection:
        assert connection.execute("SELECT message_id FROM map_applied_messages").fetchall() == [
            ("map-1",)
        ]
    assert "change_outbox_entries" not in _tables(paths.mail_db)
    assert "mail_messages" not in _tables(paths.change_outbox_db)


def test_mail_store_schema_has_no_terminal_failure_or_ack_semantics(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.project_storage import initialize_project_storage

    project_root = test_workspace / "mail-contract"
    project_root.mkdir()
    paths = initialize_project_storage(project_root, project_id="project-mail-contract")
    with closing(sqlite3.connect(paths.mail_db)) as connection:
        schema = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'mail_messages'"
            ).fetchone()[0]
        ).lower()
        default_status = next(
            row[4] for row in connection.execute("PRAGMA table_info(mail_messages)") if row[1] == "status"
        )
        row_count = connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0]

    for forbidden in ("permanent", "dead_letter", "dead-letter", "drop", "acked", "nacked"):
        assert forbidden not in schema
        assert forbidden not in str(default_status).lower()
    assert str(default_status).strip("'\"") == "pending"
    assert row_count == 0


def test_project_storage_rejects_store_kind_mismatch_and_logs_failure(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.project_storage import (
        ProjectStorageError,
        initialize_project_storage,
        initialize_project_store,
    )

    project_root = test_workspace / "storage-failure"
    project_root.mkdir()
    captured = CapturingSink()
    facade = LoggingFacade(sinks=[captured])
    paths = initialize_project_storage(
        project_root,
        project_id="project-storage-failure",
        facade=facade,
    )

    with pytest.raises(ProjectStorageError) as mismatch:
        initialize_project_store(
            paths.mail_db,
            store_kind="change_outbox",
            project_id="project-storage-failure",
            facade=facade,
        )
    assert mismatch.value.error_code == "store_kind_mismatch"
    assert _metadata(paths.mail_db)["store_kind"] == "mail"
    assert "change_outbox_entries" not in _tables(paths.mail_db)

    wrong_plan_root = test_workspace / "wrong-plan-store"
    wrong_plan_root.mkdir()
    wrong_plan_path = wrong_plan_root / ".bridle" / "plan.db"
    initialize_project_store(
        wrong_plan_path,
        store_kind="mail",
        project_id="project-wrong-plan-store",
        facade=facade,
    )
    with closing(sqlite3.connect(wrong_plan_path)) as connection:
        connection.execute(
            "INSERT INTO mail_messages("
            "message_id, source_address, target_address, message_type, payload_json, sequence_no, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sentinel-message",
                "agent://project-wrong-plan-store/agent-a/1",
                "agent://project-wrong-plan-store/agent-b/1",
                "map",
                '{"sentinel":true}',
                1,
                "2026-07-14T00:00:00.000000Z",
                "2026-07-14T00:00:00.000000Z",
            ),
        )
        connection.commit()
    before_wrong_plan = _sqlite_snapshot(wrong_plan_path, data_tables=("mail_messages",))
    before_wrong_plan_bytes = wrong_plan_path.read_bytes()

    with pytest.raises(ProjectStorageError) as plan_mismatch:
        initialize_project_storage(
            wrong_plan_root,
            project_id="project-wrong-plan-store",
            facade=facade,
        )
    assert plan_mismatch.value.error_code == "store_open_failed"
    assert wrong_plan_path.read_bytes() == before_wrong_plan_bytes
    assert _sqlite_snapshot(wrong_plan_path, data_tables=("mail_messages",)) == before_wrong_plan
    assert "map_applied_messages" not in _tables(wrong_plan_path)
    assert "plan_nodes" not in _tables(wrong_plan_path)

    broken_path = project_root / ".bridle" / "broken.db"
    broken_path.mkdir()
    with pytest.raises(ProjectStorageError) as open_failure:
        initialize_project_store(
            broken_path,
            store_kind="mail",
            project_id="project-storage-failure",
            facade=facade,
        )
    assert open_failure.value.error_code == "store_open_failed"

    failure_events = [event for event in captured.events if event.action == "project_db.open_failed"]
    assert {event.detail["store_kind"] for event in failure_events} == {
        "change_outbox",
        "mail",
        "plan",
    }
    for event in failure_events:
        payload = event.to_dict()
        assert payload["project_id"] in {
            "project-storage-failure",
            "project-wrong-plan-store",
        }
        assert payload["duration_ms"] >= 0
        assert payload["error_code"] in {"store_kind_mismatch", "store_open_failed"}
        serialized = json.dumps(payload, ensure_ascii=False)
        assert str(project_root) not in serialized
        assert str(wrong_plan_root) not in serialized
        assert "payload" not in serialized


def test_plan_store_adds_map_receipts_without_changing_existing_map_state(
    test_workspace: Path,
) -> None:
    from bridle.features.project_map.store import ProjectPlanStore

    project_root = test_workspace / "plan-receipts"
    project_root.mkdir()
    (project_root / "main.py").write_text("value = 1\n", encoding="utf-8")
    store = ProjectPlanStore(project_root, project_id="project-plan-receipts")
    store.initialize()
    before_overview = store.overview()
    before_project_id = store._metadata("project_id")
    before_scan_status = store._metadata("scan_status")

    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute("INSERT INTO map_applied_messages(message_id) VALUES (?)", ("message-1",))
        connection.commit()
    store.initialize()

    assert store._metadata("store_kind") == "plan"
    assert store._metadata("project_id") == before_project_id
    assert store._metadata("scan_status") == before_scan_status
    assert store.overview() == before_overview
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute(
            "SELECT message_id, applied_at FROM map_applied_messages"
        ).fetchone()[0] == "message-1"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO map_applied_messages(message_id) VALUES (?)",
                ("message-1",),
            )
        connection.rollback()
