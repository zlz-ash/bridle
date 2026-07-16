"""Initialize isolated project-local SQLite stores under `.bridle`."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bridle.agent.runtime.mailbox import AgentAddress, MailboxError, utc_text
from bridle.features.project_map.store import ProjectPlanStore
from bridle.logging.facade import LoggingFacade, get_logging_facade

_METADATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _strict_utc_guard_sql(value: str) -> str:
    return f"""(
        length({value}) = 27
        AND substr({value}, 5, 1) = '-'
        AND substr({value}, 8, 1) = '-'
        AND substr({value}, 11, 1) = 'T'
        AND substr({value}, 14, 1) = ':'
        AND substr({value}, 17, 1) = ':'
        AND substr({value}, 20, 1) = '.'
        AND substr({value}, 27, 1) = 'Z'
        AND substr({value}, 1, 4) NOT GLOB '*[^0-9]*'
        AND substr({value}, 6, 2) NOT GLOB '*[^0-9]*'
        AND substr({value}, 9, 2) NOT GLOB '*[^0-9]*'
        AND substr({value}, 12, 2) NOT GLOB '*[^0-9]*'
        AND substr({value}, 15, 2) NOT GLOB '*[^0-9]*'
        AND substr({value}, 18, 2) NOT GLOB '*[^0-9]*'
        AND substr({value}, 21, 6) NOT GLOB '*[^0-9]*'
        AND datetime({value}) IS NOT NULL
    )"""


def _canonical_address_guard_sql(value: str) -> str:
    byte_1 = f"substr({value}, pos + 1, 2)"
    byte_2 = f"substr({value}, pos + 4, 2)"
    byte_3 = f"substr({value}, pos + 7, 2)"
    byte_4 = f"substr({value}, pos + 10, 2)"
    hex_1 = f"{byte_1} GLOB '[0-9A-F][0-9A-F]'"
    hex_2 = f"{byte_2} GLOB '[0-9A-F][0-9A-F]'"
    hex_3 = f"{byte_3} GLOB '[0-9A-F][0-9A-F]'"
    hex_4 = f"{byte_4} GLOB '[0-9A-F][0-9A-F]'"
    continuation_2 = f"{hex_2} AND {byte_2} BETWEEN '80' AND 'BF'"
    continuation_3 = f"{hex_3} AND {byte_3} BETWEEN '80' AND 'BF'"
    continuation_4 = f"{hex_4} AND {byte_4} BETWEEN '80' AND 'BF'"
    raw = f"substr({value}, pos, 1) GLOB '[A-Za-z0-9_.~-]' OR substr({value}, pos, 1) = '/'"
    encoded_ascii = f"""(
        substr({value}, pos, 1) = '%' AND {hex_1}
        AND {byte_1} BETWEEN '00' AND '7F'
        AND {byte_1} NOT BETWEEN '30' AND '39'
        AND {byte_1} NOT BETWEEN '41' AND '5A'
        AND {byte_1} NOT BETWEEN '61' AND '7A'
        AND {byte_1} NOT IN ('2D', '2E', '5F', '7E')
    )"""
    encoded_two = f"""(
        substr({value}, pos, 1) = '%' AND {hex_1}
        AND {byte_1} BETWEEN 'C2' AND 'DF'
        AND substr({value}, pos + 3, 1) = '%' AND {continuation_2}
    )"""
    encoded_three = f"""(
        substr({value}, pos, 1) = '%' AND {hex_1}
        AND substr({value}, pos + 3, 1) = '%' AND {hex_2}
        AND substr({value}, pos + 6, 1) = '%' AND {continuation_3}
        AND (
            ({byte_1} = 'E0' AND {byte_2} BETWEEN 'A0' AND 'BF')
            OR ({byte_1} BETWEEN 'E1' AND 'EC' AND {byte_2} BETWEEN '80' AND 'BF')
            OR ({byte_1} = 'ED' AND {byte_2} BETWEEN '80' AND '9F')
            OR ({byte_1} BETWEEN 'EE' AND 'EF' AND {byte_2} BETWEEN '80' AND 'BF')
        )
    )"""
    encoded_four = f"""(
        substr({value}, pos, 1) = '%' AND {hex_1}
        AND substr({value}, pos + 3, 1) = '%' AND {hex_2}
        AND substr({value}, pos + 6, 1) = '%' AND {continuation_3}
        AND substr({value}, pos + 9, 1) = '%' AND {continuation_4}
        AND (
            ({byte_1} = 'F0' AND {byte_2} BETWEEN '90' AND 'BF')
            OR ({byte_1} BETWEEN 'F1' AND 'F3' AND {byte_2} BETWEEN '80' AND 'BF')
            OR ({byte_1} = 'F4' AND {byte_2} BETWEEN '80' AND '8F')
        )
    )"""
    step = f"""CASE
        WHEN {raw} THEN pos + 1
        WHEN {encoded_ascii} THEN pos + 3
        WHEN {encoded_two} THEN pos + 6
        WHEN {encoded_three} THEN pos + 9
        WHEN {encoded_four} THEN pos + 12
        ELSE NULL
    END"""
    tail = f"substr({value}, 9)"
    first_slash = f"instr({tail}, '/')"
    return f"""(
        substr({value}, 1, 8) = 'agent://'
        AND length({value}) - length(replace({value}, '/', '')) = 4
        AND {first_slash} > 1
        AND instr(substr({tail}, {first_slash} + 1), '/') > 1
        AND substr(rtrim({value}, '0123456789'), -1, 1) = '/'
        AND CAST(substr({value}, length(rtrim({value}, '0123456789')) + 1) AS INTEGER) >= 1
        AND printf('%d', CAST(substr({value}, length(rtrim({value}, '0123456789')) + 1) AS INTEGER))
            = substr({value}, length(rtrim({value}, '0123456789')) + 1)
        AND EXISTS (
            WITH RECURSIVE scan(pos) AS (
                VALUES(9)
                UNION ALL
                SELECT {step}
                FROM scan
                WHERE pos <= length({value})
            )
            SELECT 1 FROM scan WHERE pos = length({value}) + 1
        )
    )"""

_MAIL_TABLE_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS mail_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    source_address TEXT NOT NULL CHECK (
        source_address GLOB 'agent://*/*/*'
        AND length(source_address) - length(replace(source_address, '/', '')) = 4
        AND source_address NOT GLOB '*%[a-f][0-9A-Fa-f]*'
        AND source_address NOT GLOB '*%[0-9A-Fa-f][a-f]*'
        AND substr(rtrim(source_address, '0123456789'), -1, 1) = '/'
        AND CAST(substr(source_address, length(rtrim(source_address, '0123456789')) + 1) AS INTEGER) >= 1
        AND printf('%d', CAST(substr(source_address, length(rtrim(source_address, '0123456789')) + 1) AS INTEGER))
            = substr(source_address, length(rtrim(source_address, '0123456789')) + 1)
    ),
    target_address TEXT NOT NULL CHECK (
        target_address GLOB 'agent://*/*/*'
        AND length(target_address) - length(replace(target_address, '/', '')) = 4
        AND target_address NOT GLOB '*%[a-f][0-9A-Fa-f]*'
        AND target_address NOT GLOB '*%[0-9A-Fa-f][a-f]*'
        AND substr(rtrim(target_address, '0123456789'), -1, 1) = '/'
        AND CAST(substr(target_address, length(rtrim(target_address, '0123456789')) + 1) AS INTEGER) >= 1
        AND printf('%d', CAST(substr(target_address, length(rtrim(target_address, '0123456789')) + 1) AS INTEGER))
            = substr(target_address, length(rtrim(target_address, '0123456789')) + 1)
    ),
    message_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (
        json_valid(payload_json) AND json(payload_json) = payload_json
    ),
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'retry_wait', 'delivered')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_retry_at TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL CHECK (
        length(created_at) = 27 AND substr(created_at, 27, 1) = 'Z'
        AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND datetime(created_at) IS NOT NULL
    ),
    updated_at TEXT NOT NULL CHECK (
        length(updated_at) = 27 AND substr(updated_at, 27, 1) = 'Z'
        AND substr(updated_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND datetime(updated_at) IS NOT NULL
    ),
    CHECK (
        (status = 'pending' AND next_retry_at IS NULL AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (status = 'retry_wait' AND next_retry_at IS NOT NULL
            AND length(next_retry_at) = 27 AND substr(next_retry_at, 27, 1) = 'Z'
            AND substr(next_retry_at, 21, 6) NOT GLOB '*[^0-9]*'
            AND datetime(next_retry_at) IS NOT NULL AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (status = 'leased' AND next_retry_at IS NULL AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
            AND length(lease_expires_at) = 27 AND substr(lease_expires_at, 27, 1) = 'Z'
            AND substr(lease_expires_at, 21, 6) NOT GLOB '*[^0-9]*'
            AND datetime(lease_expires_at) IS NOT NULL)
        OR (status = 'delivered' AND next_retry_at IS NULL AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);
"""

_MAIL_V2_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_sequence_no
ON mail_messages(sequence_no);
CREATE INDEX IF NOT EXISTS mail_messages_delivery_idx
ON mail_messages(status, next_retry_at, sequence_no);
CREATE INDEX IF NOT EXISTS mail_messages_lease_idx
ON mail_messages(status, lease_expires_at, sequence_no);
"""

_MAIL_V2_GUARDS = f"""
CREATE TRIGGER IF NOT EXISTS mail_messages_v2_insert_guard
BEFORE INSERT ON mail_messages
WHEN NOT (
    NEW.message_id != ''
    AND NEW.source_address GLOB 'agent://*/*/*'
    AND length(NEW.source_address) - length(replace(NEW.source_address, '/', '')) = 4
    AND NEW.source_address NOT GLOB '*%[a-f][0-9A-Fa-f]*'
    AND NEW.source_address NOT GLOB '*%[0-9A-Fa-f][a-f]*'
    AND substr(rtrim(NEW.source_address, '0123456789'), -1, 1) = '/'
    AND CAST(substr(NEW.source_address, length(rtrim(NEW.source_address, '0123456789')) + 1) AS INTEGER) >= 1
    AND printf('%d', CAST(substr(NEW.source_address, length(rtrim(NEW.source_address, '0123456789')) + 1) AS INTEGER))
        = substr(NEW.source_address, length(rtrim(NEW.source_address, '0123456789')) + 1)
    AND {_canonical_address_guard_sql("NEW.source_address")}
    AND NEW.target_address GLOB 'agent://*/*/*'
    AND length(NEW.target_address) - length(replace(NEW.target_address, '/', '')) = 4
    AND NEW.target_address NOT GLOB '*%[a-f][0-9A-Fa-f]*'
    AND NEW.target_address NOT GLOB '*%[0-9A-Fa-f][a-f]*'
    AND substr(rtrim(NEW.target_address, '0123456789'), -1, 1) = '/'
    AND CAST(substr(NEW.target_address, length(rtrim(NEW.target_address, '0123456789')) + 1) AS INTEGER) >= 1
    AND printf('%d', CAST(substr(NEW.target_address, length(rtrim(NEW.target_address, '0123456789')) + 1) AS INTEGER))
        = substr(NEW.target_address, length(rtrim(NEW.target_address, '0123456789')) + 1)
    AND {_canonical_address_guard_sql("NEW.target_address")}
    AND NEW.message_type != ''
    AND json_valid(NEW.payload_json) AND json(NEW.payload_json) = NEW.payload_json
    AND NEW.sequence_no > 0
    AND NEW.attempt >= 0
    AND NEW.status IN ('pending', 'leased', 'retry_wait', 'delivered')
    AND length(NEW.created_at) = 27 AND substr(NEW.created_at, 27, 1) = 'Z'
    AND datetime(NEW.created_at) IS NOT NULL
    AND {_strict_utc_guard_sql("NEW.created_at")}
    AND length(NEW.updated_at) = 27 AND substr(NEW.updated_at, 27, 1) = 'Z'
    AND datetime(NEW.updated_at) IS NOT NULL
    AND {_strict_utc_guard_sql("NEW.updated_at")}
    AND (
        (NEW.status = 'pending' AND NEW.next_retry_at IS NULL
            AND NEW.lease_owner IS NULL AND NEW.lease_token IS NULL
            AND NEW.lease_expires_at IS NULL)
        OR (NEW.status = 'retry_wait' AND NEW.next_retry_at IS NOT NULL
            AND datetime(NEW.next_retry_at) IS NOT NULL AND NEW.lease_owner IS NULL
            AND {_strict_utc_guard_sql("NEW.next_retry_at")}
            AND NEW.lease_token IS NULL AND NEW.lease_expires_at IS NULL)
        OR (NEW.status = 'leased' AND NEW.next_retry_at IS NULL
            AND NEW.lease_owner IS NOT NULL AND NEW.lease_token IS NOT NULL
            AND NEW.lease_expires_at IS NOT NULL AND datetime(NEW.lease_expires_at) IS NOT NULL)
            AND {_strict_utc_guard_sql("NEW.lease_expires_at")}
        OR (NEW.status = 'delivered' AND NEW.next_retry_at IS NULL
            AND NEW.lease_owner IS NULL AND NEW.lease_token IS NULL
            AND NEW.lease_expires_at IS NULL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'mail_schema_invalid');
END;

CREATE TRIGGER IF NOT EXISTS mail_messages_v2_update_guard
BEFORE UPDATE ON mail_messages
WHEN NOT (
    NEW.message_id != ''
    AND NEW.source_address GLOB 'agent://*/*/*'
    AND length(NEW.source_address) - length(replace(NEW.source_address, '/', '')) = 4
    AND NEW.source_address NOT GLOB '*%[a-f][0-9A-Fa-f]*'
    AND NEW.source_address NOT GLOB '*%[0-9A-Fa-f][a-f]*'
    AND substr(rtrim(NEW.source_address, '0123456789'), -1, 1) = '/'
    AND CAST(substr(NEW.source_address, length(rtrim(NEW.source_address, '0123456789')) + 1) AS INTEGER) >= 1
    AND printf('%d', CAST(substr(NEW.source_address, length(rtrim(NEW.source_address, '0123456789')) + 1) AS INTEGER))
        = substr(NEW.source_address, length(rtrim(NEW.source_address, '0123456789')) + 1)
    AND {_canonical_address_guard_sql("NEW.source_address")}
    AND NEW.target_address GLOB 'agent://*/*/*'
    AND length(NEW.target_address) - length(replace(NEW.target_address, '/', '')) = 4
    AND NEW.target_address NOT GLOB '*%[a-f][0-9A-Fa-f]*'
    AND NEW.target_address NOT GLOB '*%[0-9A-Fa-f][a-f]*'
    AND substr(rtrim(NEW.target_address, '0123456789'), -1, 1) = '/'
    AND CAST(substr(NEW.target_address, length(rtrim(NEW.target_address, '0123456789')) + 1) AS INTEGER) >= 1
    AND printf('%d', CAST(substr(NEW.target_address, length(rtrim(NEW.target_address, '0123456789')) + 1) AS INTEGER))
        = substr(NEW.target_address, length(rtrim(NEW.target_address, '0123456789')) + 1)
    AND {_canonical_address_guard_sql("NEW.target_address")}
    AND NEW.message_type != ''
    AND json_valid(NEW.payload_json) AND json(NEW.payload_json) = NEW.payload_json
    AND NEW.sequence_no > 0
    AND NEW.attempt >= 0
    AND NEW.status IN ('pending', 'leased', 'retry_wait', 'delivered')
    AND length(NEW.created_at) = 27 AND substr(NEW.created_at, 27, 1) = 'Z'
    AND datetime(NEW.created_at) IS NOT NULL
    AND {_strict_utc_guard_sql("NEW.created_at")}
    AND length(NEW.updated_at) = 27 AND substr(NEW.updated_at, 27, 1) = 'Z'
    AND datetime(NEW.updated_at) IS NOT NULL
    AND {_strict_utc_guard_sql("NEW.updated_at")}
    AND (
        (NEW.status = 'pending' AND NEW.next_retry_at IS NULL
            AND NEW.lease_owner IS NULL AND NEW.lease_token IS NULL
            AND NEW.lease_expires_at IS NULL)
        OR (NEW.status = 'retry_wait' AND NEW.next_retry_at IS NOT NULL
            AND datetime(NEW.next_retry_at) IS NOT NULL AND NEW.lease_owner IS NULL
            AND {_strict_utc_guard_sql("NEW.next_retry_at")}
            AND NEW.lease_token IS NULL AND NEW.lease_expires_at IS NULL)
        OR (NEW.status = 'leased' AND NEW.next_retry_at IS NULL
            AND NEW.lease_owner IS NOT NULL AND NEW.lease_token IS NOT NULL
            AND NEW.lease_expires_at IS NOT NULL AND datetime(NEW.lease_expires_at) IS NOT NULL)
            AND {_strict_utc_guard_sql("NEW.lease_expires_at")}
        OR (NEW.status = 'delivered' AND NEW.next_retry_at IS NULL
            AND NEW.lease_owner IS NULL AND NEW.lease_token IS NULL
            AND NEW.lease_expires_at IS NULL)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'mail_schema_invalid');
END;
"""

_CHANGE_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS change_outbox_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    before_sha256 TEXT NOT NULL,
    after_sha256 TEXT NOT NULL,
    staging_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_STORE_SCHEMAS = {
    "change_outbox": _CHANGE_OUTBOX_SCHEMA,
}

_LEGACY_MAIL_COLUMNS = {
    "id",
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
}


@dataclass(frozen=True)
class ProjectStoragePaths:
    mail_db: Path
    change_outbox_db: Path
    plan_db: Path


class ProjectStorageError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in connection.execute("SELECT key, value FROM metadata").fetchall()
    }


@dataclass(frozen=True)
class _MailMigrationRow:
    row_id: int
    status: str
    source_address: str
    target_address: str
    payload_json: str
    next_retry_at: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str


def _migration_error() -> ProjectStorageError:
    return ProjectStorageError("schema_migration_unsupported")


def _canonical_legacy_address(value: object, project_id: str) -> str:
    text = str(value)
    try:
        return AgentAddress.parse(text).to_uri()
    except MailboxError:
        if text.startswith("agent://"):
            agent_id = text.removeprefix("agent://")
            if agent_id and "/" not in agent_id:
                try:
                    return AgentAddress(project_id, agent_id, 1).to_uri()
                except MailboxError as exc:
                    raise _migration_error() from exc
        raise _migration_error() from None


def _canonical_json(value: object) -> str:
    try:
        parsed = json.loads(str(value))
        return json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _migration_error() from exc


def _canonical_timestamp(value: object | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise _migration_error()
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _migration_error() from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return utc_text(parsed)


def _validate_legacy_state(row: sqlite3.Row) -> str:
    status = str(row["status"])
    retry = row["next_retry_at"]
    lease_values = (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
    if status == "pending":
        valid = all(value is None for value in lease_values)
        normalized = "retry_wait" if retry is not None else "pending"
    elif status == "delivered":
        valid = retry is None and all(value is None for value in lease_values)
        normalized = "delivered"
    elif status == "retry_wait":
        valid = retry is not None and all(value is None for value in lease_values)
        normalized = "retry_wait"
    elif status == "leased":
        valid = retry is None and all(value is not None for value in lease_values)
        normalized = "leased"
    else:
        valid = False
        normalized = status
    if not valid:
        raise _migration_error()
    return normalized


def _preflight_mail_database(path: Path, *, project_id: str) -> list[_MailMigrationRow] | None:
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=0)) as connection:
            connection.row_factory = sqlite3.Row
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "metadata" not in table_names or "mail_messages" not in table_names:
                raise _migration_error()
            stored = _metadata(connection)
            if stored.get("store_kind") != "mail":
                raise ProjectStorageError("store_kind_mismatch")
            if stored.get("project_id") != project_id:
                raise ProjectStorageError("project_id_mismatch")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(mail_messages)")
            }
            if columns != _LEGACY_MAIL_COLUMNS:
                raise _migration_error()
            if version == 2:
                return None
            if version != 0:
                raise _migration_error()
            rows = connection.execute("SELECT * FROM mail_messages ORDER BY id").fetchall()
    except ProjectStorageError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise _migration_error() from exc

    sequences: set[int] = set()
    migrations: list[_MailMigrationRow] = []
    for row in rows:
        try:
            row_id = int(row["id"])
            sequence_no = int(row["sequence_no"])
            attempt = int(row["attempt"])
        except (TypeError, ValueError) as exc:
            raise _migration_error() from exc
        if row_id < 1 or sequence_no < 1 or attempt < 0 or sequence_no in sequences:
            raise _migration_error()
        if not str(row["message_id"]) or not str(row["message_type"]):
            raise _migration_error()
        sequences.add(sequence_no)
        normalized_status = _validate_legacy_state(row)
        migrations.append(
            _MailMigrationRow(
                row_id=row_id,
                status=normalized_status,
                source_address=_canonical_legacy_address(row["source_address"], project_id),
                target_address=_canonical_legacy_address(row["target_address"], project_id),
                payload_json=_canonical_json(row["payload_json"]),
                next_retry_at=_canonical_timestamp(row["next_retry_at"], required=False),
                lease_expires_at=_canonical_timestamp(row["lease_expires_at"], required=False),
                created_at=str(_canonical_timestamp(row["created_at"], required=True)),
                updated_at=str(_canonical_timestamp(row["updated_at"], required=True)),
            )
        )
    return migrations


def _initialize_new_mail_database(connection: sqlite3.Connection) -> None:
    connection.executescript(_MAIL_TABLE_SCHEMA_V2)
    connection.executescript(_MAIL_V2_INDEXES)
    connection.executescript(_MAIL_V2_GUARDS)
    connection.execute("PRAGMA user_version = 2")


def _migrate_mail_database(
    connection: sqlite3.Connection,
    migrations: list[_MailMigrationRow],
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in migrations:
            connection.execute(
                """
                UPDATE mail_messages
                SET status=?, source_address=?, target_address=?, payload_json=?, next_retry_at=?,
                    lease_expires_at=?, created_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    row.status,
                    row.source_address,
                    row.target_address,
                    row.payload_json,
                    row.next_retry_at,
                    row.lease_expires_at,
                    row.created_at,
                    row.updated_at,
                    row.row_id,
                ),
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_sequence_no "
            "ON mail_messages(sequence_no)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS mail_messages_delivery_idx ON mail_messages("
            "status, next_retry_at, sequence_no)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS mail_messages_lease_idx ON mail_messages("
            "status, lease_expires_at, sequence_no)"
        )
        connection.executescript(_MAIL_V2_GUARDS)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise


def initialize_project_store(
    database_path: str | Path,
    *,
    store_kind: str,
    project_id: str,
    facade: LoggingFacade | None = None,
) -> Path:
    """Create or validate one responsibility-specific project database."""
    started = time.perf_counter()
    logging_facade = facade or get_logging_facade()
    path = Path(database_path)
    try:
        if store_kind != "mail" and store_kind not in _STORE_SCHEMAS:
            raise ProjectStorageError("unsupported_store_kind")
        existed = path.is_file()
        mail_migrations: list[_MailMigrationRow] | None = None
        if store_kind == "mail" and existed:
            mail_migrations = _preflight_mail_database(path, project_id=project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(_METADATA_SCHEMA)
            stored = _metadata(connection)
            if "store_kind" in stored and stored["store_kind"] != store_kind:
                raise ProjectStorageError("store_kind_mismatch")
            if "project_id" in stored and stored["project_id"] != project_id:
                raise ProjectStorageError("project_id_mismatch")
            if store_kind == "mail":
                if not existed:
                    _initialize_new_mail_database(connection)
                elif mail_migrations is not None:
                    _migrate_mail_database(connection, mail_migrations)
            else:
                connection.executescript(_STORE_SCHEMAS[store_kind])
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('store_kind', ?)",
                (store_kind,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('project_id', ?)",
                (project_id,),
            )
            connection.commit()
    except ProjectStorageError as exc:
        logging_facade.error_event(
            "project_db.open_failed",
            "failed",
            project_id=project_id,
            error_code=exc.error_code,
            duration_ms=_elapsed_ms(started),
            detail={"store_kind": store_kind},
        )
        raise
    except (OSError, sqlite3.Error) as exc:
        error = ProjectStorageError("store_open_failed")
        logging_facade.error_event(
            "project_db.open_failed",
            "failed",
            project_id=project_id,
            error_code=error.error_code,
            duration_ms=_elapsed_ms(started),
            detail={"store_kind": store_kind},
        )
        raise error from exc

    logging_facade.info_event(
        "project_db.initialized",
        "completed",
        project_id=project_id,
        duration_ms=_elapsed_ms(started),
        detail={"store_kind": store_kind},
    )
    return path


def initialize_project_storage(
    project_root: str | Path,
    *,
    project_id: str,
    facade: LoggingFacade | None = None,
) -> ProjectStoragePaths:
    """Initialize the isolated mail, change-outbox, and plan stores for one project."""
    root = Path(project_root).resolve()
    storage_root = root / ".bridle"
    logging_facade = facade or get_logging_facade()
    paths = ProjectStoragePaths(
        mail_db=storage_root / "mail.db",
        change_outbox_db=storage_root / "change_outbox.db",
        plan_db=storage_root / "plan.db",
    )
    initialize_project_store(
        paths.mail_db,
        store_kind="mail",
        project_id=project_id,
        facade=logging_facade,
    )
    initialize_project_store(
        paths.change_outbox_db,
        store_kind="change_outbox",
        project_id=project_id,
        facade=logging_facade,
    )
    started = time.perf_counter()
    try:
        ProjectPlanStore(root, project_id=project_id, facade=logging_facade).initialize()
    except Exception as exc:
        error = ProjectStorageError("store_open_failed")
        logging_facade.error_event(
            "project_db.open_failed",
            "failed",
            project_id=project_id,
            error_code=error.error_code,
            duration_ms=_elapsed_ms(started),
            detail={"store_kind": "plan"},
        )
        raise error from exc
    logging_facade.info_event(
        "project_db.initialized",
        "completed",
        project_id=project_id,
        duration_ms=_elapsed_ms(started),
        detail={"store_kind": "plan"},
    )
    return paths
