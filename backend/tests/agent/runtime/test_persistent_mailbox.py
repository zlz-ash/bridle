from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from typing import Any

import pytest

from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class CapturingSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


class FailingSink:
    def emit(self, event: LogEvent) -> None:
        raise RuntimeError("secret sink exception body")


def _api() -> tuple[Any, Any, Any, Any]:
    from bridle.agent.runtime.mailbox import AgentAddress, MailboxError, MailEnvelope
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox

    return AgentAddress, MailEnvelope, MailboxError, PersistentMailbox


def _address(project: str = "project-1", agent: str = "agent-1", generation: int = 1) -> Any:
    AgentAddress, _, _, _ = _api()
    return AgentAddress(project_id=project, agent_id=agent, generation=generation)


def _envelope(
    message_id: str,
    *,
    source: Any | None = None,
    target: Any | None = None,
    payload: Any | None = None,
    message_type: str = "TaskAssigned",
) -> Any:
    _, MailEnvelope, _, _ = _api()
    return MailEnvelope(
        message_id=message_id,
        message_type=message_type,
        source=source or _address(agent="parent"),
        target=target or _address(agent="child"),
        payload={"value": message_id} if payload is None else payload,
    )


def _mailbox(
    path: Path,
    *,
    project_id: str = "project-1",
    consumer_id: str = "consumer-1",
    capacity: int = 100,
    clock: MutableClock | None = None,
    facade: LoggingFacade | None = None,
    empty_wait_hook: Any | None = None,
    lease_seconds: float = 10,
    retry_base_seconds: float = 2,
    retry_max_seconds: float = 8,
    trace_id: str | None = None,
) -> Any:
    _, _, _, PersistentMailbox = _api()
    optional: dict[str, Any] = {}
    if trace_id is not None:
        optional["trace_id"] = trace_id
    return PersistentMailbox(
        path,
        project_id=project_id,
        consumer_id=consumer_id,
        capacity=capacity,
        lease_seconds=lease_seconds,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        busy_timeout_ms=20,
        clock=clock,
        facade=facade,
        empty_wait_hook=empty_wait_hook,
        **optional,
    )


def _row(path: Path, message_id: str) -> sqlite3.Row:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM mail_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    assert row is not None
    return row


def _legacy_database(path: Path, project_id: str, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE mail_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                source_address TEXT NOT NULL,
                target_address TEXT NOT NULL,
                message_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("store_kind", "mail"), ("project_id", project_id)),
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO mail_messages(
                    id, message_id, source_address, target_address, message_type,
                    payload_json, sequence_no, status, attempt, next_retry_at,
                    lease_owner, lease_token, lease_expires_at, created_at, updated_at
                ) VALUES (
                    :id, :message_id, :source_address, :target_address, :message_type,
                    :payload_json, :sequence_no, :status, :attempt, :next_retry_at,
                    :lease_owner, :lease_token, :lease_expires_at, :created_at, :updated_at
                )
                """,
                row,
            )
        connection.commit()


def _legacy_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "message_id": "legacy-1",
        "source_address": "agent://parent",
        "target_address": "agent://child",
        "message_type": "TaskAssigned",
        "payload_json": '{"z": 1, "a": "中文"}',
        "sequence_no": 1,
        "status": "pending",
        "attempt": 0,
        "next_retry_at": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "created_at": "2026-07-14 01:02:03",
        "updated_at": "2026-07-14T01:02:03+00:00",
    }
    row.update(changes)
    return row


def _schema_snapshot(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            "master": connection.execute(
                "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
            "rows": connection.execute("SELECT * FROM mail_messages ORDER BY id").fetchall(),
            "metadata": connection.execute("SELECT * FROM metadata ORDER BY key").fetchall(),
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
        }


def test_agent_address_round_trips_canonical_percent_encoded_components() -> None:
    assert importlib.util.find_spec("bridle.agent.runtime.mailbox") is not None, (
        "target behavior missing: canonical AgentAddress module"
    )
    AgentAddress, _, MailboxError, _ = _api()

    address = AgentAddress(project_id="project / 中文", agent_id="child / α", generation=7)
    canonical = "agent://project%20%2F%20%E4%B8%AD%E6%96%87/child%20%2F%20%CE%B1/7"
    assert address.to_uri() == canonical
    assert AgentAddress.parse(canonical) == address
    assert AgentAddress.parse(address.to_uri()).to_uri() == canonical

    invalid = (
        "agent:///child/1",
        "agent://project//1",
        "agent://project/child/0",
        "agent://project/child/not-a-generation",
        "agent://project%2fchild/agent/1",
        "agent://legacy-short",
    )
    for value in invalid:
        with pytest.raises(MailboxError, match="invalid_address"):
            AgentAddress.parse(value)


def test_mail_envelope_keeps_payload_separate_and_canonical() -> None:
    _, MailEnvelope, MailboxError, _ = _api()
    envelope = MailEnvelope(
        message_id="message-1",
        message_type="TaskAssigned",
        source=_address(agent="parent"),
        target=_address(agent="child"),
        payload={"z": [3, 2, 1], "a": "中文"},
    )

    assert envelope.payload_json == '{"a":"中文","z":[3,2,1]}'
    assert json.loads(envelope.payload_json) == {"a": "中文", "z": [3, 2, 1]}
    for forbidden in ("sequence", "attempt", "lease", "token", "retry", ".bridle"):
        assert forbidden not in envelope.payload_json
    assert envelope.message_id == "message-1"
    assert envelope.source == _address(agent="parent")

    with pytest.raises(MailboxError, match="invalid_payload"):
        MailEnvelope(
            message_id="bad-set",
            message_type="TaskAssigned",
            source=_address(agent="parent"),
            target=_address(agent="child"),
            payload={"not-json": {1, 2}},
        )
    with pytest.raises(MailboxError, match="invalid_payload"):
        MailEnvelope(
            message_id="bad-number",
            message_type="TaskAssigned",
            source=_address(agent="parent"),
            target=_address(agent="child"),
            payload={"number": float("nan")},
        )


def test_mail_schema_v2_enforces_delivery_contract(test_workspace: Path) -> None:
    from bridle.agent.runtime.project_storage import initialize_project_storage

    project_root = test_workspace / "schema-v2"
    project_root.mkdir()
    path = initialize_project_storage(project_root, project_id="project-1").mail_db

    assert path.drive.upper() == "D:"
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]: {"type": row[2], "not_null": row[3], "default": row[4], "pk": row[5]}
            for row in connection.execute("PRAGMA table_info(mail_messages)")
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(mail_messages)")
        }
        schema = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='mail_messages'"
            ).fetchone()[0]
        ).lower()

        assert version == 2
        assert columns["created_at"]["default"] is None
        assert columns["updated_at"]["default"] is None
        assert {
            "uq_mail_sequence_no",
            "mail_messages_delivery_idx",
            "mail_messages_lease_idx",
        } <= indexes
        index_columns = {
            name: [
                row[2]
                for row in connection.execute(f"PRAGMA index_info('{name}')")
            ]
            for name in indexes
        }
        assert index_columns["mail_messages_delivery_idx"] == [
            "status",
            "next_retry_at",
            "sequence_no",
        ]
        assert index_columns["mail_messages_lease_idx"] == [
            "status",
            "lease_expires_at",
            "sequence_no",
        ]
        assert "check" in schema
        for forbidden in ("permanent", "dead_letter", "dead-letter", "drop", "acked", "nacked"):
            assert forbidden not in schema

        valid = (
            "valid-1",
            "agent://project-1/parent/1",
            "agent://project-1/child/1",
            "TaskAssigned",
            "{}",
            1,
            "pending",
            0,
            None,
            None,
            None,
            None,
            "2026-07-14T01:02:03.000000Z",
            "2026-07-14T01:02:03.000000Z",
        )
        sql = (
            "INSERT INTO mail_messages(message_id, source_address, target_address, message_type, "
            "payload_json, sequence_no, status, attempt, next_retry_at, lease_owner, lease_token, "
            "lease_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        connection.execute(sql, valid)
        invalid_rows = (
            valid[:1] + ("agent://short",) + valid[2:],
            valid[:1] + ("agent:///child/1",) + valid[2:],
            valid[:1] + ("agent://project-1//1",) + valid[2:],
            valid[:1] + ("agent://project 1/child/1",) + valid[2:],
            valid[:1] + ("agent://project%ZZ/child/1",) + valid[2:],
            valid[:1] + ("agent://project%41/child/1",) + valid[2:],
            valid[:1] + ("agent://project%FF/child/1",) + valid[2:],
            valid[:1] + ("agent://project-1/child/1suffix",) + valid[2:],
            valid[:1] + ("agent://project%2fchild/child/1",) + valid[2:],
            valid[:4] + ("not-json",) + valid[5:],
            valid[:4] + ('{ "value": 1 }',) + valid[5:],
            valid[:5] + (0,) + valid[6:],
            valid[:7] + (-1,) + valid[8:],
            valid[:6] + ("failed",) + valid[7:],
            valid[:8] + ("2026-07-14T01:02:03.000000Z",) + valid[9:],
            valid[:12] + ("bad-time",) + valid[13:],
            (
                "bad-retry-time",
                *valid[1:6],
                "retry_wait",
                1,
                "2026-07-14T01:02:03Z",
                None,
                None,
                None,
                *valid[12:],
            ),
            (
                "bad-lease-time",
                *valid[1:6],
                "leased",
                1,
                None,
                "owner",
                "token",
                "2026-07-14T01:02:03Z",
                *valid[12:],
            ),
        )
        for index, candidate in enumerate(invalid_rows, start=2):
            mutable = list(candidate)
            mutable[0] = f"invalid-{index}"
            if mutable[5] == 1:
                mutable[5] = index
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, tuple(mutable))
        invalid_updates = (
            ("source_address", "agent://project%41/parent/1"),
            ("target_address", "agent://project%ZZ/child/1"),
            ("payload_json", '{ "value": 1 }'),
            ("created_at", "2026-07-14T01:02:03Z"),
        )
        for column, value in invalid_updates:
            try:
                connection.execute(
                    f"UPDATE mail_messages SET {column}=? WHERE message_id='valid-1'",
                    (value,),
                )
            except sqlite3.IntegrityError:
                continue
            pytest.fail(f"accepted non-canonical update: {column}={value}")
        connection.rollback()


def test_legacy_mail_schema_migrates_in_place_and_is_idempotent(test_workspace: Path) -> None:
    from bridle.agent.runtime.project_storage import initialize_project_storage

    project_root = test_workspace / "legacy-success"
    path = project_root / ".bridle" / "mail.db"
    rows = [
        _legacy_row(id=10, message_id="pending", sequence_no=10),
        _legacy_row(
            id=11,
            message_id="retry",
            sequence_no=11,
            status="pending",
            attempt=2,
            next_retry_at="2026-07-14 01:03:03",
        ),
        _legacy_row(
            id=12,
            message_id="leased",
            sequence_no=12,
            status="leased",
            attempt=3,
            lease_owner="consumer-a",
            lease_token="legacy-token",
            lease_expires_at="2026-07-14T01:04:03Z",
        ),
    ]
    _legacy_database(path, "project-legacy", rows)
    with closing(sqlite3.connect(path)) as connection:
        rootpage_before = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='mail_messages'"
        ).fetchone()[0]

    initialize_project_storage(project_root, project_id="project-legacy")
    with closing(sqlite3.connect(path)) as connection:
        rootpage_after = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='mail_messages'"
        ).fetchone()[0]
        migrated = connection.execute(
            "SELECT id, message_id, source_address, target_address, payload_json, sequence_no, "
            "status, attempt, next_retry_at, lease_owner, lease_token, lease_expires_at, "
            "created_at, updated_at FROM mail_messages ORDER BY id"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert rootpage_after == rootpage_before
    assert version == 2
    assert [row[:2] for row in migrated] == [(10, "pending"), (11, "retry"), (12, "leased")]
    assert [row[5:8] for row in migrated] == [
        (10, "pending", 0),
        (11, "retry_wait", 2),
        (12, "leased", 3),
    ]
    for row in migrated:
        assert row[2] == "agent://project-legacy/parent/1"
        assert row[3] == "agent://project-legacy/child/1"
        assert row[4] == '{"a":"中文","z":1}'
        assert row[12].endswith(".000000Z")
        assert row[13].endswith(".000000Z")

    with (
        closing(sqlite3.connect(path)) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "UPDATE mail_messages SET source_address=? WHERE message_id='pending'",
            ("agent://project%41/parent/1",),
        )

    before_second_initialize = _schema_snapshot(path)
    initialize_project_storage(project_root, project_id="project-legacy")
    assert _schema_snapshot(path) == before_second_initialize


def test_unsupported_legacy_mail_rows_leave_database_byte_identical(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.project_storage import ProjectStorageError, initialize_project_storage

    invalid_changes = (
        {"status": "failed"},
        {"payload_json": "not-json"},
        {"source_address": "invalid-address"},
        {"created_at": "not-a-time"},
        {"sequence_no": 0},
        {"status": "leased", "lease_owner": "owner", "lease_token": None, "lease_expires_at": None},
    )
    for index, changes in enumerate(invalid_changes):
        project_root = test_workspace / f"legacy-failure-{index}"
        path = project_root / ".bridle" / "mail.db"
        _legacy_database(path, "project-legacy", [_legacy_row(**changes)])
        before_bytes = path.read_bytes()
        before_snapshot = _schema_snapshot(path)

        with pytest.raises(ProjectStorageError, match="schema_migration_unsupported") as exc:
            initialize_project_storage(project_root, project_id="project-legacy")

        assert exc.value.error_code == "schema_migration_unsupported"
        assert path.read_bytes() == before_bytes
        assert _schema_snapshot(path) == before_snapshot
        assert not path.with_name("mail.db-wal").exists()
        assert not path.with_name("mail.db-shm").exists()

    special_cases = ("unknown-column", "unknown-version", "duplicate-sequence")
    for name in special_cases:
        project_root = test_workspace / f"legacy-failure-{name}"
        path = project_root / ".bridle" / "mail.db"
        rows = [_legacy_row()]
        if name == "duplicate-sequence":
            rows.append(_legacy_row(id=2, message_id="legacy-2"))
        _legacy_database(path, "project-legacy", rows)
        with closing(sqlite3.connect(path)) as connection:
            if name == "unknown-column":
                connection.execute("ALTER TABLE mail_messages ADD COLUMN unknown_value TEXT")
            elif name == "unknown-version":
                connection.execute("PRAGMA user_version = 7")
            connection.commit()
        before_bytes = path.read_bytes()
        before_snapshot = _schema_snapshot(path)

        with pytest.raises(ProjectStorageError, match="schema_migration_unsupported"):
            initialize_project_storage(project_root, project_id="project-legacy")

        assert path.read_bytes() == before_bytes
        assert _schema_snapshot(path) == before_snapshot


def test_enqueue_capacity_is_atomic_and_duplicate_stays_idempotent(test_workspace: Path) -> None:
    path = test_workspace / "enqueue" / ".bridle" / "mail.db"
    mailbox = _mailbox(path, capacity=2)
    assert mailbox.enqueue(_envelope("existing")).status == "inserted"
    barrier = Barrier(2)

    def enqueue(message_id: str) -> Any:
        barrier.wait(timeout=2)
        return _mailbox(path, capacity=2, consumer_id=message_id).enqueue(_envelope(message_id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(enqueue, ("concurrent-a", "concurrent-b")))

    assert sorted(outcome.status for outcome in outcomes) == ["backpressure", "inserted"]
    with closing(sqlite3.connect(path)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM mail_messages WHERE status != 'delivered'"
        ).fetchone()[0]
        sequences = [
            row[0]
            for row in connection.execute("SELECT sequence_no FROM mail_messages ORDER BY sequence_no")
        ]
    assert count == 2
    assert sequences == [1, 2]
    duplicate = _mailbox(path, capacity=2, consumer_id="duplicate").enqueue(
        _envelope("existing", payload={"different": True})
    )
    assert duplicate.status == "existing"
    assert duplicate.sequence_no == 1

    occupied_path = test_workspace / "enqueue-occupied" / ".bridle" / "mail.db"
    occupied = _mailbox(occupied_path, capacity=2)
    occupied.enqueue(_envelope("leased-capacity"))
    occupied.enqueue(_envelope("retry-capacity"))
    leased = occupied.claim(_address(agent="child"))
    retry = occupied.claim(_address(agent="child"))
    assert leased.status == retry.status == "claimed"
    assert occupied.nack(
        "retry-capacity", retry.lease_token, target=_address(agent="child")
    ).status == "nacked"
    rejected = occupied.enqueue(_envelope("over-capacity"))
    assert rejected.status == "backpressure"
    assert _row(occupied_path, "leased-capacity")["status"] == "leased"
    assert _row(occupied_path, "retry-capacity")["status"] == "retry_wait"


def test_concurrent_claim_has_one_winner_and_preserves_sequence_order(
    test_workspace: Path,
) -> None:
    path = test_workspace / "claim" / ".bridle" / "mail.db"
    producer = _mailbox(path, consumer_id="producer")
    producer.enqueue(_envelope("first"))
    barrier = Barrier(2)

    def claim(consumer_id: str) -> Any:
        barrier.wait(timeout=2)
        return _mailbox(path, consumer_id=consumer_id).claim(_address(agent="child"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ("consumer-a", "consumer-b")))

    assert sum(outcome.status == "claimed" for outcome in outcomes) == 1
    assert all(outcome.status in {"claimed", "empty", "mailbox_busy"} for outcome in outcomes)
    row = _row(path, "first")
    assert row["status"] == "leased"
    assert row["attempt"] == 1
    assert row["lease_owner"] in {"consumer-a", "consumer-b"}

    winner = next(outcome for outcome in outcomes if outcome.status == "claimed")
    owner = _mailbox(path, consumer_id=row["lease_owner"])
    assert owner.ack(
        "first", winner.lease_token, target=_address(agent="child")
    ).status == "acked"
    producer.enqueue(_envelope("second"))
    producer.enqueue(_envelope("third"))
    ordered = _mailbox(path, consumer_id="ordered")
    second = ordered.claim(_address(agent="child"))
    assert (second.message_id, second.sequence_no) == ("second", 2)
    ordered.ack("second", second.lease_token, target=_address(agent="child"))
    third = ordered.claim(_address(agent="child"))
    assert (third.message_id, third.sequence_no) == ("third", 3)


def test_expired_lease_fences_old_token_owner_and_generation(test_workspace: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 14, tzinfo=UTC))
    path = test_workspace / "fencing" / ".bridle" / "mail.db"
    producer = _mailbox(path, clock=clock, consumer_id="producer")
    producer.enqueue(_envelope("lease-me"))
    consumer_a = _mailbox(path, clock=clock, consumer_id="consumer-a")
    first = consumer_a.claim(_address(agent="child"))
    assert first.status == "claimed"
    clock.advance(11)
    consumer_b = _mailbox(path, clock=clock, consumer_id="consumer-b")
    second = consumer_b.claim(_address(agent="child"))
    assert second.status == "claimed"
    assert second.lease_token != first.lease_token
    before = tuple(_row(path, "lease-me"))

    rejected = (
        consumer_a.renew("lease-me", first.lease_token, target=_address(agent="child")),
        consumer_a.ack("lease-me", first.lease_token, target=_address(agent="child")),
        consumer_a.nack("lease-me", first.lease_token, target=_address(agent="child")),
        consumer_b.renew("lease-me", second.lease_token, target=_address(agent="child", generation=2)),
        _mailbox(path, clock=clock, consumer_id="wrong-owner").ack(
            "lease-me", second.lease_token, target=_address(agent="child")
        ),
    )
    assert {result.status for result in rejected} == {"lost_lease"}
    assert tuple(_row(path, "lease-me")) == before

    assert consumer_b.renew(
        "lease-me", second.lease_token, target=_address(agent="child")
    ).status == "renewed"
    nacked = consumer_b.nack(
        "lease-me", second.lease_token, target=_address(agent="child")
    )
    assert nacked.status == "nacked"
    clock.value = nacked.next_retry_at
    third = consumer_b.claim(_address(agent="child"))
    assert third.status == "claimed"
    assert consumer_b.ack(
        "lease-me", third.lease_token, target=_address(agent="child")
    ).status == "acked"
    delivered = _row(path, "lease-me")
    assert delivered["status"] == "delivered"
    assert all(
        delivered[field] is None
        for field in ("next_retry_at", "lease_owner", "lease_token", "lease_expires_at")
    )


def test_claimed_and_delivered_states_survive_mailbox_restart(test_workspace: Path) -> None:
    clock = MutableClock(datetime(2026, 7, 14, tzinfo=UTC))
    path = test_workspace / "restart" / ".bridle" / "mail.db"
    first_instance = _mailbox(path, clock=clock, consumer_id="consumer-a")
    first_instance.enqueue(_envelope("restart-message"))
    first = first_instance.claim(_address(agent="child"))
    assert first.envelope is not None
    assert first.envelope.message_id == "restart-message"
    assert first.envelope.sequence_no == 1
    assert first.envelope.attempt == 1
    assert first.envelope.created_at == datetime(2026, 7, 14, tzinfo=UTC)
    assert first.envelope.updated_at == datetime(2026, 7, 14, tzinfo=UTC)
    assert all(
        forbidden not in first.envelope.payload_json
        for forbidden in ("sequence", "attempt", "created_at", "updated_at", "lease", "token")
    )
    before_restart = tuple(_row(path, "restart-message"))

    second_instance = _mailbox(path, clock=clock, consumer_id="consumer-b")
    assert second_instance.claim(_address(agent="child")).status == "empty"
    assert tuple(_row(path, "restart-message")) == before_restart
    clock.advance(11)
    second = second_instance.claim(_address(agent="child"))
    assert second.status == "claimed"
    assert second.lease_token != first.lease_token
    assert second.envelope is not None
    assert second.envelope.sequence_no == 1
    assert second.envelope.attempt == 2
    assert second.envelope.created_at == first.envelope.created_at
    assert second.envelope.updated_at == clock.value
    assert first_instance.ack(
        "restart-message", first.lease_token, target=_address(agent="child")
    ).status == "lost_lease"
    assert second_instance.ack(
        "restart-message", second.lease_token, target=_address(agent="child")
    ).status == "acked"

    third_instance = _mailbox(path, clock=clock, consumer_id="consumer-c")
    assert third_instance.claim(_address(agent="child")).status == "empty"
    row = _row(path, "restart-message")
    assert row["status"] == "delivered"
    assert row["message_id"] == "restart-message"
    assert row["sequence_no"] == 1
    assert row["attempt"] == 2
    assert all(
        row[field] is None
        for field in ("next_retry_at", "lease_owner", "lease_token", "lease_expires_at")
    )


@pytest.mark.asyncio
async def test_nack_retries_indefinitely_with_capped_exponential_backoff(
    test_workspace: Path,
) -> None:
    clock = MutableClock(datetime(2026, 7, 14, tzinfo=UTC))
    path = test_workspace / "retry" / ".bridle" / "mail.db"
    mailbox = _mailbox(path, clock=clock)
    mailbox.enqueue(_envelope("retry-forever"))

    expected_delays = (2, 4, 8, 8, 8, 8)
    for attempt, delay in enumerate(expected_delays, start=1):
        claimed = mailbox.claim(_address(agent="child"))
        assert claimed.status == "claimed"
        assert claimed.attempt == attempt
        nacked = mailbox.nack(
            "retry-forever", claimed.lease_token, target=_address(agent="child")
        )
        assert nacked.status == "nacked"
        assert nacked.next_retry_at == clock.value + timedelta(seconds=delay)
        assert mailbox.claim(_address(agent="child")).status == "empty"
        clock.value = nacked.next_retry_at

    row = _row(path, "retry-forever")
    assert row["status"] == "retry_wait"
    assert row["attempt"] == len(expected_delays)
    assert row["message_id"] == "retry-forever"

    retry_path = test_workspace / "retry-due-wakeup" / ".bridle" / "mail.db"
    retry_mailbox = _mailbox(
        retry_path,
        retry_base_seconds=0.02,
        retry_max_seconds=0.02,
    )
    retry_mailbox.enqueue(_envelope("retry-due"))
    retry_claim = retry_mailbox.claim(_address(agent="child"))
    retry_mailbox.nack(
        "retry-due", retry_claim.lease_token, target=_address(agent="child")
    )
    retried_without_notification = await retry_mailbox.receive(
        _address(agent="child"), timeout=1
    )
    assert retried_without_notification.status == "claimed"
    assert retried_without_notification.message_id == "retry-due"
    assert retried_without_notification.attempt == 2

    lease_path = test_workspace / "lease-due-wakeup" / ".bridle" / "mail.db"
    lease_owner = _mailbox(lease_path, consumer_id="lease-owner", lease_seconds=0.02)
    lease_owner.enqueue(_envelope("lease-due"))
    assert lease_owner.claim(_address(agent="child")).status == "claimed"
    lease_recoverer = _mailbox(
        lease_path,
        consumer_id="lease-recoverer",
        lease_seconds=0.02,
    )
    recovered_without_notification = await lease_recoverer.receive(
        _address(agent="child"), timeout=1
    )
    assert recovered_without_notification.status == "claimed"
    assert recovered_without_notification.message_id == "lease-due"
    assert recovered_without_notification.attempt == 2


def test_sqlite_busy_returns_stable_result_without_losing_messages(test_workspace: Path) -> None:
    path = test_workspace / "busy" / ".bridle" / "mail.db"
    mailbox = _mailbox(path)
    assert mailbox.enqueue(_envelope("preserved")).status == "inserted"
    with closing(sqlite3.connect(path, timeout=0)) as locked:
        locked.execute("BEGIN IMMEDIATE")
        busy_enqueue = mailbox.enqueue(_envelope("blocked"))
        busy_claim = mailbox.claim(_address(agent="child"))
        assert busy_enqueue.status == "mailbox_busy"
        assert busy_claim.status == "mailbox_busy"
        assert str(path.resolve()) not in repr(busy_enqueue)
        assert str(path.resolve()) not in repr(busy_claim)
        locked.rollback()

    assert mailbox.enqueue(_envelope("blocked")).status == "inserted"
    first = mailbox.claim(_address(agent="child"))
    assert (first.status, first.message_id, first.sequence_no) == ("claimed", "preserved", 1)
    mailbox.ack("preserved", first.lease_token, target=_address(agent="child"))
    second = mailbox.claim(_address(agent="child"))
    assert (second.status, second.message_id, second.sequence_no) == ("claimed", "blocked", 2)
    for result in (
        mailbox.enqueue(_envelope("capacity-check")),
        mailbox.claim(_address(agent="missing")),
        first,
        second,
    ):
        assert str(path.resolve()) not in repr(result)


@pytest.mark.asyncio
async def test_close_releases_only_all_leases_owned_by_that_consumer(
    test_workspace: Path,
) -> None:
    path = test_workspace / "close-owner" / ".bridle" / "mail.db"
    producer = _mailbox(path, consumer_id="producer")
    for message_id in ("a-1", "a-2", "b-1"):
        producer.enqueue(_envelope(message_id))
    consumer_a = _mailbox(path, consumer_id="consumer-a")
    consumer_b = _mailbox(path, consumer_id="consumer-b")
    claims_a = [consumer_a.claim(_address(agent="child")) for _ in range(2)]
    claim_b = consumer_b.claim(_address(agent="child"))
    before_a = {
        message_id: tuple(_row(path, message_id))
        for message_id in ("a-1", "a-2")
    }
    before_b = tuple(_row(path, "b-1"))

    with closing(sqlite3.connect(path, timeout=0)) as locked:
        locked.execute("BEGIN IMMEDIATE")
        busy_close = await consumer_a.close()
        assert busy_close.status == "mailbox_busy"
        assert consumer_a.claim(_address(agent="child")).status == "mailbox_busy"
        assert {
            message_id: tuple(_row(path, message_id))
            for message_id in ("a-1", "a-2")
        } == before_a
        locked.rollback()

    closed = await consumer_a.close()
    assert closed.status == "closed"
    assert (await consumer_a.close()).status == "closed"
    assert tuple(_row(path, "b-1")) == before_b
    assert consumer_b.renew(
        "b-1", claim_b.lease_token, target=_address(agent="child")
    ).status == "renewed"

    consumer_c = _mailbox(path, consumer_id="consumer-c")
    reclaimed = [consumer_c.claim(_address(agent="child")) for _ in range(2)]
    assert [result.message_id for result in reclaimed] == ["a-1", "a-2"]
    assert all(result.status == "claimed" for result in reclaimed)
    assert all(_row(path, result.message_id)["attempt"] == 2 for result in reclaimed)
    assert consumer_b.ack(
        "b-1", claim_b.lease_token, target=_address(agent="child")
    ).status == "acked"
    assert {claim.message_id for claim in claims_a} == {"a-1", "a-2"}


@pytest.mark.asyncio
async def test_mailbox_events_are_correlated_sequenced_redacted_and_sink_safe(
    test_workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    captured = CapturingSink()
    facade = LoggingFacade(sinks=[FailingSink(), captured])
    clock = MutableClock(datetime(2026, 7, 14, tzinfo=UTC))
    path = test_workspace / "secret-absolute-path" / ".bridle" / "mail.db"
    mailbox = _mailbox(
        path,
        capacity=1,
        clock=clock,
        facade=facade,
        trace_id="trace-mail",
    )
    with caplog.at_level("ERROR", logger="bridle.logging"):
            thread_outcomes: list[str] = []

            def enqueue_from_background() -> None:
                result = mailbox.enqueue(
                    _envelope("secret-message", payload={"secret": "payload-secret"})
                )
                thread_outcomes.append(result.status)

            thread = Thread(target=enqueue_from_background)
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive()
            assert thread_outcomes == ["inserted"]
            assert mailbox.enqueue(_envelope("backpressure-message")).status == "backpressure"
            claimed = mailbox.claim(_address(agent="child"))
            raw_token = claimed.lease_token
            mailbox.ack("secret-message", "wrong-token", target=_address(agent="child"))
            renewed = mailbox.renew(
                "secret-message", raw_token, target=_address(agent="child")
            )
            assert renewed.status == "renewed"
            nacked = mailbox.nack(
                "secret-message", raw_token, target=_address(agent="child")
            )
            clock.value = nacked.next_retry_at
            retried = mailbox.claim(_address(agent="child"))
            mailbox.ack("secret-message", retried.lease_token, target=_address(agent="child"))
            mailbox.notify()
            await mailbox.close()

    mail_events = [event for event in captured.events if event.action.startswith("mail.")]
    actions = {event.action for event in mail_events}
    assert {
        "mail.enqueued",
        "mail.backpressure",
        "mail.claimed",
        "mail.lease_lost",
        "mail.renewed",
        "mail.nacked",
        "mail.retry_scheduled",
        "mail.acked",
        "mail.wakeup",
        "mail.closed",
    } <= actions
    serialized = json.dumps([event.to_dict() for event in mail_events], ensure_ascii=False)
    assert "payload-secret" not in serialized
    assert raw_token not in serialized
    assert str(path.parent.parent) not in serialized
    assert "secret sink exception body" not in serialized
    expected_token_digests = {
        hashlib.sha256(token.encode()).hexdigest()[:12]
        for token in (raw_token, "wrong-token", retried.lease_token)
    }
    observed_token_digests: set[str] = set()
    for event in mail_events:
        payload = event.to_dict()
        assert payload["trace_id"] == "trace-mail"
        assert payload["project_id"] == "project-1"
        assert payload["agent_id"] is not None
        assert payload["generation"] is not None
        assert "attempt" in payload["detail"]
        assert "sequence_no" in payload["detail"]
        digest = payload["detail"].get("lease_token_digest")
        if digest is not None:
            assert len(digest) == 12
            assert digest in expected_token_digests
            observed_token_digests.add(digest)
    assert observed_token_digests == expected_token_digests
    enqueued = next(
        event for event in mail_events
        if event.action == "mail.enqueued" and event.message_id == "secret-message"
    )
    claimed_event = next(
        event for event in mail_events
        if event.action == "mail.claimed" and event.message_id == "secret-message"
    )
    assert enqueued.detail["sequence_no"] == 1
    assert claimed_event.detail["sequence_no"] == 1
    assert claimed_event.detail["attempt"] == 1


@pytest.mark.asyncio
async def test_parent_child_and_map_addresses_share_one_project_mail_database(
    test_workspace: Path,
) -> None:
    path = test_workspace / "shared-project" / ".bridle" / "mail.db"
    clock = MutableClock(datetime(2026, 7, 14, tzinfo=UTC))
    parent = _mailbox(path, project_id="shared", consumer_id="parent", clock=clock)
    child = _mailbox(path, project_id="shared", consumer_id="child", clock=clock)
    map_runtime = _mailbox(path, project_id="shared", consumer_id="map", clock=clock)
    target = _address(project="shared", agent="project-runtime")
    other_target = _address(project="shared", agent="other-runtime")
    parent.enqueue(
        _envelope(
            "from-parent",
            source=_address(project="shared", agent="parent"),
            target=target,
        )
    )
    child.enqueue(
        _envelope(
            "from-child",
            source=_address(project="shared", agent="child"),
            target=other_target,
        )
    )
    map_runtime.enqueue(
        _envelope(
            "from-map",
            source=_address(project="shared", agent="map"),
            target=target,
        )
    )

    receiver = _mailbox(
        path,
        project_id="shared",
        consumer_id="receiver",
        clock=clock,
    )
    claimed = [receiver.claim(target) for _ in range(2)]
    assert [result.message_id for result in claimed] == ["from-parent", "from-map"]
    assert receiver.claim(target).status == "empty"
    other = receiver.claim(other_target)
    assert other.message_id == "from-child"
    claimed.append(other)
    assert len(list(path.parent.glob("mail.db"))) == 1
    assert {
        _row(path, result.message_id)["target_address"] for result in claimed
    } == {target.to_uri(), other_target.to_uri()}
    for result in claimed:
        assert result.envelope is not None
        receiver.nack(
            result.message_id,
            result.lease_token,
            target=result.envelope.target,
        )
    await receiver.close()
    clock.advance(3)
    restarted = _mailbox(
        path,
        project_id="shared",
        consumer_id="restarted",
        clock=clock,
    )
    assert _row(path, "from-parent")["status"] == "retry_wait"
    assert path.resolve() == restarted.database_path
    assert [restarted.claim(target).message_id for _ in range(2)] == [
        "from-parent",
        "from-map",
    ]
    assert restarted.claim(target).status == "empty"
    assert restarted.claim(other_target).message_id == "from-child"
