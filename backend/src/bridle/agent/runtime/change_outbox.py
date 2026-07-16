"""Reliable per-project CodeChanged outbox and atomic single-file commit boundary."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import math
import os
import secrets
import sqlite3
import textwrap
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.logging.facade import LoggingFacade, get_logging_facade

if TYPE_CHECKING:
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox

ChangeType = Literal["add", "modify", "remove"]
OutboxState = Literal["RESERVED", "COMMITTING", "READY", "DELIVERED", "REBASE_REQUIRED"]
FailureHook = Callable[[str, "ChangeIntent"], None]
Clock = Callable[[], datetime]

_MISSING_DIGEST = "missing:"
_ACTIVE_STATES = ("RESERVED", "COMMITTING", "READY", "REBASE_REQUIRED")
_PATH_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_intents (
    message_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    change_type TEXT NOT NULL,
    before_digest TEXT NOT NULL,
    after_digest TEXT NOT NULL,
    staging_path TEXT,
    state TEXT NOT NULL,
    fence_token INTEGER NOT NULL,
    superseded_by TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    trace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_intents_state
ON change_intents(state, superseded_by, created_at);
CREATE INDEX IF NOT EXISTS idx_change_intents_path
ON change_intents(relative_path, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_change_intents_path_inflight
ON change_intents(relative_path)
WHERE superseded_by IS NULL AND state IN ('RESERVED', 'COMMITTING', 'REBASE_REQUIRED');
"""


class ChangeOutboxError(RuntimeError):
    """Stable failure returned by the change commit boundary."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class ChangeCorrelation:
    trace_id: str
    project_id: str
    agent_id: str
    generation: int

    def __post_init__(self) -> None:
        if not self.trace_id or not self.project_id or not self.agent_id or self.generation < 1:
            raise ChangeOutboxError("invalid_correlation")


@dataclass(frozen=True)
class ChangeIntent:
    message_id: str
    relative_path: str
    change_type: ChangeType
    before_digest: str
    after_digest: str
    staging_path: str | None
    state: OutboxState
    fence_token: int
    superseded_by: str | None
    attempt: int
    next_retry_at: str | None
    correlation: ChangeCorrelation


class _ChangeOutboxIterationError(RuntimeError):
    def __init__(self, intent: ChangeIntent, cause: Exception) -> None:
        super().__init__(str(cause))
        self.intent = intent
        self.error_code = type(cause).__name__


@dataclass(frozen=True)
class ChangeOutboxResult:
    status: str
    intent: ChangeIntent | None = None
    error_code: str | None = None


class ChangeOutbox:
    """Own one project's `.bridle/change_outbox.db`."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        project_id: str,
        capacity: int = 1000,
        busy_timeout_ms: int = 100,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        clock: Clock | None = None,
        facade: LoggingFacade | None = None,
        failure_hook: FailureHook | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        if (
            not project_id
            or capacity < 1
            or retry_base_seconds <= 0
            or retry_max_seconds < retry_base_seconds
        ):
            raise ChangeOutboxError("invalid_outbox_config")
        self.project_id = project_id
        self.capacity = capacity
        self.busy_timeout_ms = max(0, busy_timeout_ms)
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self.database_path = self.project_root / ".bridle" / "change_outbox.db"
        self._facade = facade or get_logging_facade()
        self._failure_hook = failure_hook
        self._initialize()

    def intents(self) -> list[ChangeIntent]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM change_intents ORDER BY created_at, message_id"
            ).fetchall()
        return [self._intent(row) for row in rows]

    def get(self, message_id: str) -> ChangeIntent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM change_intents WHERE message_id = ?", (message_id,)
            ).fetchone()
        return None if row is None else self._intent(row)

    def recover(self) -> list[ChangeIntent]:
        """Recover startup leftovers before the project accepts new formal writers."""
        recovered: list[ChangeIntent] = []
        for snapshot in self.intents():
            with self.path_lock(snapshot.relative_path):
                intent = self.get(snapshot.message_id)
                if intent is None:
                    continue
                if intent.superseded_by is not None:
                    self._cleanup_staging(intent)
                    continue
                if intent.state == "RESERVED":
                    with closing(self._connect()) as connection:
                        cursor = connection.execute(
                            "DELETE FROM change_intents "
                            "WHERE message_id = ? AND state = 'RESERVED' "
                            "AND fence_token = ? AND superseded_by IS NULL",
                            (intent.message_id, intent.fence_token),
                        )
                        connection.commit()
                    if cursor.rowcount == 1:
                        self._cleanup_staging(intent)
                        self._log("change_outbox.recovered", "reserved_abandoned", intent)
                    continue
                if intent.state == "READY":
                    self._cleanup_staging(intent)
                    continue
                if intent.state != "COMMITTING":
                    continue
                owned = self._take_fence(intent)
                if owned is None:
                    continue
                result = self._recover_committing(owned)
                if result is not None:
                    recovered.append(result)
        return recovered

    def publish_ready(
        self,
        mailbox: PersistentMailbox,
        *,
        target: AgentAddress | None = None,
    ) -> list[ChangeOutboxResult]:
        results: list[ChangeOutboxResult] = []
        destination = target or AgentAddress(self.project_id, "map-runtime", 1)
        for intent in self.intents():
            if intent.state != "READY" or intent.superseded_by is not None:
                continue
            now = self._now()
            if intent.next_retry_at is not None and intent.next_retry_at > _utc_text(now):
                continue
            started = time.perf_counter()
            envelope = MailEnvelope(
                message_id=intent.message_id,
                message_type="CodeChanged",
                source=AgentAddress(self.project_id, "change-outbox", 1),
                target=destination,
                payload={
                    "path": intent.relative_path,
                    "before_digest": intent.before_digest,
                    "after_digest": intent.after_digest,
                },
            )
            mail_result = mailbox.enqueue(envelope)
            if mail_result.status in {"inserted", "existing"}:
                try:
                    self.call_hook("after_mail_enqueue", intent)
                except Exception as exc:
                    raise _ChangeOutboxIterationError(intent, exc) from exc
                delivered = self._transition(
                    intent,
                    state="DELIVERED",
                    expected_state="READY",
                )
                if delivered is not None:
                    self._log(
                        "change_outbox.delivered",
                        "completed",
                        delivered,
                        duration_ms=_duration_ms(started),
                    )
                    results.append(ChangeOutboxResult("delivered", delivered))
                continue
            retried = self._increment_attempt(intent, now=now)
            self._log(
                "change_outbox.publish_retry",
                "scheduled",
                retried,
                attempt=retried.attempt,
                error_code=mail_result.status,
                duration_ms=_duration_ms(started),
            )
            results.append(
                ChangeOutboxResult("publish_retry", retried, error_code=mail_result.status)
            )
        return results

    def reserve(
        self,
        relative_path: str,
        *,
        change_type: ChangeType,
        before_digest: str,
        after_digest: str,
        staging_path: str | None,
        correlation: ChangeCorrelation,
    ) -> ChangeOutboxResult:
        normalized = self._normalize_path(relative_path)
        message_id = f"codechanged-{secrets.token_hex(16)}"
        now = _utc_text()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                blocker = connection.execute(
                    """
                    SELECT * FROM change_intents
                    WHERE relative_path = ? AND superseded_by IS NULL
                      AND state IN ('RESERVED', 'COMMITTING', 'REBASE_REQUIRED')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (normalized,),
                ).fetchone()
                active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM change_intents
                        WHERE superseded_by IS NULL
                          AND state IN ('RESERVED', 'COMMITTING', 'READY', 'REBASE_REQUIRED')
                        """
                    ).fetchone()[0]
                )
                replaced_count = (
                    1 if blocker is not None and blocker["state"] == "REBASE_REQUIRED" else 0
                )
                if active_count - replaced_count >= self.capacity:
                    connection.rollback()
                    return ChangeOutboxResult("backpressure", error_code="outbox_capacity")
                if blocker is not None and blocker["state"] != "REBASE_REQUIRED":
                    connection.rollback()
                    return ChangeOutboxResult("path_busy", error_code="outbox_path_busy")
                max_fence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(fence_token), 0) FROM change_intents WHERE relative_path = ?",
                        (normalized,),
                    ).fetchone()[0]
                )
                if blocker is not None:
                    connection.execute(
                        "UPDATE change_intents SET superseded_by = ?, updated_at = ? "
                        "WHERE message_id = ? AND state = 'REBASE_REQUIRED' AND superseded_by IS NULL",
                        (message_id, now, blocker["message_id"]),
                    )
                connection.execute(
                    """
                    INSERT INTO change_intents(
                        message_id, relative_path, change_type, before_digest, after_digest,
                        staging_path, state, fence_token, superseded_by, attempt,
                        next_retry_at, trace_id, project_id, agent_id, generation,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?, NULL, 0, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        normalized,
                        change_type,
                        before_digest,
                        after_digest,
                        staging_path,
                        max_fence + 1,
                        correlation.trace_id,
                        correlation.project_id,
                        correlation.agent_id,
                        correlation.generation,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM change_intents WHERE message_id = ?", (message_id,)
                ).fetchone()
                connection.commit()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                return ChangeOutboxResult("outbox_busy", error_code="outbox_busy")
            raise ChangeOutboxError("outbox_storage_error") from exc
        assert row is not None
        intent = self._intent(row)
        if blocker is not None:
            old = self.get(str(blocker["message_id"]))
            if old is not None:
                self._log("change_outbox.superseded", "completed", old)
        self._log("change_outbox.reserved", "completed", intent)
        return ChangeOutboxResult("reserved", intent)

    def mark_committing(self, intent: ChangeIntent) -> ChangeIntent | None:
        changed = self._transition(
            intent,
            state="COMMITTING",
            expected_state="RESERVED",
        )
        if changed is not None:
            self._log("change_outbox.committing", "completed", changed)
        return changed

    def mark_ready(self, intent: ChangeIntent) -> ChangeIntent | None:
        changed = self._transition(
            intent,
            state="READY",
            expected_state="COMMITTING",
        )
        if changed is not None:
            self._log("change_outbox.ready", "completed", changed)
        return changed

    def abandon_reserved(self, intent: ChangeIntent) -> None:
        self._cleanup_staging(intent)
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM change_intents WHERE message_id = ? AND state = 'RESERVED' AND fence_token = ?",
                (intent.message_id, intent.fence_token),
            )
            connection.commit()

    def call_hook(self, stage: str, intent: ChangeIntent) -> None:
        if self._failure_hook is not None:
            self._failure_hook(stage, intent)

    def _recover_committing(self, intent: ChangeIntent) -> ChangeIntent | None:
        target = self._target(intent.relative_path)
        current = file_digest(target)
        if current == intent.after_digest or (
            current == intent.before_digest and self._replay(intent, target)
        ):
            ready = self.mark_ready(intent)
        else:
            ready = self._transition(
                intent,
                state="REBASE_REQUIRED",
                expected_state="COMMITTING",
            )
            if ready is not None:
                self._log("change_outbox.rebase_required", "blocked", ready)
        if ready is not None:
            self._cleanup_staging(ready)
            self._log("change_outbox.recovered", "completed", ready)
        return ready

    def _replay(self, intent: ChangeIntent, target: Path) -> bool:
        staging = self._staging(intent)
        if staging is None:
            return False
        try:
            if intent.change_type == "remove":
                if not target.is_file() or staging.exists():
                    return False
                os.replace(target, staging)
                return file_digest(target) == intent.after_digest
            if file_digest(staging) != intent.after_digest:
                return False
            os.replace(staging, target)
            return file_digest(target) == intent.after_digest
        except OSError:
            return False

    def _take_fence(self, intent: ChangeIntent) -> ChangeIntent | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            next_token = intent.fence_token + 1
            cursor = connection.execute(
                """
                UPDATE change_intents SET fence_token = ?, updated_at = ?
                WHERE message_id = ? AND state = 'COMMITTING'
                  AND superseded_by IS NULL AND fence_token = ?
                """,
                (next_token, _utc_text(), intent.message_id, intent.fence_token),
            )
            connection.commit()
        if cursor.rowcount != 1:
            return None
        return self.get(intent.message_id)

    def _transition(
        self,
        intent: ChangeIntent,
        *,
        state: OutboxState,
        expected_state: OutboxState,
    ) -> ChangeIntent | None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE change_intents SET state = ?, updated_at = ?
                WHERE message_id = ? AND state = ? AND fence_token = ? AND superseded_by IS NULL
                """,
                (state, _utc_text(), intent.message_id, expected_state, intent.fence_token),
            )
            connection.commit()
        if cursor.rowcount != 1:
            return None
        return self.get(intent.message_id)

    def _increment_attempt(self, intent: ChangeIntent, *, now: datetime) -> ChangeIntent:
        next_attempt = intent.attempt + 1
        if self.retry_base_seconds >= self.retry_max_seconds:
            delay_seconds = self.retry_max_seconds
        else:
            saturation_exponent = math.ceil(
                math.log2(self.retry_max_seconds / self.retry_base_seconds)
            )
            exponent = min(max(0, next_attempt - 1), saturation_exponent)
            delay_seconds = min(
                self.retry_base_seconds * (2**exponent),
                self.retry_max_seconds,
            )
        next_retry_at = _utc_text(now + timedelta(seconds=delay_seconds))
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE change_intents "
                "SET attempt = attempt + 1, next_retry_at = ?, updated_at = ? "
                "WHERE message_id = ? AND state = 'READY'",
                (next_retry_at, _utc_text(now), intent.message_id),
            )
            connection.commit()
        updated = self.get(intent.message_id)
        assert updated is not None
        return updated

    def _cleanup_staging(self, intent: ChangeIntent) -> None:
        staging = self._staging(intent)
        if staging is not None:
            with suppress(OSError):
                staging.unlink(missing_ok=True)

    def _staging(self, intent: ChangeIntent) -> Path | None:
        if not intent.staging_path:
            return None
        return self.project_root.joinpath(*intent.staging_path.split("/"))

    def _target(self, relative_path: str) -> Path:
        return self.project_root.joinpath(*relative_path.split("/"))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def path_lock(self, relative_path: str) -> threading.RLock:
        key = (str(self.database_path), self._normalize_path(relative_path))
        with _PATH_LOCKS_GUARD:
            return _PATH_LOCKS.setdefault(key, threading.RLock())

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.is_file():
            try:
                with closing(self._connect()) as connection:
                    existing = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'project_id'"
                    ).fetchone()
                    if existing is not None and str(existing[0]) != self.project_id:
                        raise ChangeOutboxError("outbox_project_mismatch")
                    if existing is not None:
                        columns = {
                            str(row[1])
                            for row in connection.execute(
                                "PRAGMA table_info(change_intents)"
                            ).fetchall()
                        }
                        if "next_retry_at" not in columns:
                            connection.execute(
                                "ALTER TABLE change_intents ADD COLUMN next_retry_at TEXT"
                            )
                            connection.commit()
                        return
            except sqlite3.OperationalError as exc:
                if _is_busy(exc):
                    return
                raise ChangeOutboxError("outbox_storage_error") from exc
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            existing = connection.execute(
                "SELECT value FROM metadata WHERE key = 'project_id'"
            ).fetchone()
            if existing is not None and str(existing[0]) != self.project_id:
                raise ChangeOutboxError("outbox_project_mismatch")
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('store_kind', 'change_outbox')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('project_id', ?)",
                (self.project_id,),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _intent(row: sqlite3.Row) -> ChangeIntent:
        return ChangeIntent(
            message_id=str(row["message_id"]),
            relative_path=str(row["relative_path"]),
            change_type=str(row["change_type"]),  # type: ignore[arg-type]
            before_digest=str(row["before_digest"]),
            after_digest=str(row["after_digest"]),
            staging_path=None if row["staging_path"] is None else str(row["staging_path"]),
            state=str(row["state"]),  # type: ignore[arg-type]
            fence_token=int(row["fence_token"]),
            superseded_by=None if row["superseded_by"] is None else str(row["superseded_by"]),
            attempt=int(row["attempt"]),
            next_retry_at=(
                None if row["next_retry_at"] is None else str(row["next_retry_at"])
            ),
            correlation=ChangeCorrelation(
                trace_id=str(row["trace_id"]),
                project_id=str(row["project_id"]),
                agent_id=str(row["agent_id"]),
                generation=int(row["generation"]),
            ),
        )

    @staticmethod
    def _normalize_path(value: str) -> str:
        raw = str(value).strip()
        path = PurePosixPath(raw)
        if (
            not raw
            or raw.startswith("/")
            or "\\" in raw
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise ChangeOutboxError("path_outside_workspace")
        normalized = path.as_posix()
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized == ".bridle" or normalized.startswith(".bridle/"):
            raise ChangeOutboxError("path_denied")
        return normalized

    def _log(
        self,
        action: str,
        status: str,
        intent: ChangeIntent,
        *,
        attempt: int | None = None,
        error_code: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        detail = {
            "path": intent.relative_path,
            "before_digest": intent.before_digest,
            "after_digest": intent.after_digest,
        }
        if attempt is not None:
            detail["attempt"] = attempt
        self._facade.info_event(
            action,
            status,
            trace_id=intent.correlation.trace_id,
            message_id=intent.message_id,
            project_id=intent.correlation.project_id,
            agent_id=intent.correlation.agent_id,
            generation=intent.correlation.generation,
            duration_ms=max(0, duration_ms),
            attempt=attempt,
            error_code=error_code,
            detail=detail,
        )


class AtomicPatchCommitter:
    """Commit one validated file change and its durable Outbox fact."""

    def __init__(self, outbox: ChangeOutbox) -> None:
        self.outbox = outbox

    def commit(
        self,
        relative_path: str,
        *,
        change_type: ChangeType,
        new_text: str | None,
        correlation: ChangeCorrelation,
    ) -> ChangeOutboxResult:
        normalized = self.outbox._normalize_path(relative_path)
        lock = self.outbox.path_lock(normalized)
        if not lock.acquire(blocking=False):
            return ChangeOutboxResult("path_busy", error_code="outbox_path_busy")
        try:
            return self._commit_locked(
                normalized,
                change_type=change_type,
                new_text=new_text,
                correlation=correlation,
            )
        finally:
            lock.release()

    def _commit_locked(
        self,
        relative_path: str,
        *,
        change_type: ChangeType,
        new_text: str | None,
        correlation: ChangeCorrelation,
    ) -> ChangeOutboxResult:
        target = self.outbox.project_root.joinpath(*relative_path.split("/"))
        before = file_digest(target)
        if change_type == "remove":
            after = missing_digest()
            suffix = f".bridle-tombstone-{secrets.token_hex(8)}"
        else:
            if new_text is None:
                return ChangeOutboxResult("failed", error_code="missing_patch_text")
            after = _text_digest(new_text)
            suffix = f".bridle-stage-{secrets.token_hex(8)}"
        staging = target.with_name(f".{target.name}{suffix}")
        staging_rel = staging.relative_to(self.outbox.project_root).as_posix()
        reserved = self.outbox.reserve(
            relative_path,
            change_type=change_type,
            before_digest=before,
            after_digest=after,
            staging_path=staging_rel,
            correlation=correlation,
        )
        if reserved.status != "reserved" or reserved.intent is None:
            return reserved
        intent = reserved.intent
        self.outbox.call_hook("after_reserved", intent)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if change_type != "remove":
                assert new_text is not None
                with staging.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(new_text)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.outbox.call_hook("after_staging_fsync", intent)
            committing = self.outbox.mark_committing(intent)
            if committing is None:
                return ChangeOutboxResult("path_busy", error_code="outbox_fence_lost")
            intent = committing
            self.outbox.call_hook("after_committing", intent)
            if change_type == "remove":
                os.replace(target, staging)
            else:
                os.replace(staging, target)
            self.outbox._log("change_outbox.write_committed", "completed", intent)
            self.outbox.call_hook("after_replace", intent)
            ready = self.outbox.mark_ready(intent)
            if ready is None:
                return ChangeOutboxResult("failed", error_code="outbox_fence_lost")
            self.outbox._cleanup_staging(ready)
            self.outbox.call_hook("after_ready", ready)
            return ChangeOutboxResult("ready", ready)
        except OSError as exc:
            current = self.outbox.get(intent.message_id)
            if current is not None and current.state == "RESERVED":
                self.outbox.abandon_reserved(current)
            return ChangeOutboxResult("failed", current, error_code=type(exc).__name__)


class ChangeOutboxForwarder:
    """Publish durable READY intents until an owning runtime stops the task."""

    def __init__(
        self,
        outbox: ChangeOutbox,
        mailbox: PersistentMailbox,
        *,
        poll_seconds: float = 0.1,
        wake_callback: Callable[[ChangeIntent], Awaitable[None]] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ChangeOutboxError("invalid_forwarder_config")
        self.outbox = outbox
        self.mailbox = mailbox
        self.poll_seconds = poll_seconds
        self.wake_callback = wake_callback
        self._pending_wakes: dict[str, ChangeIntent] = {}

    def run_once(self) -> list[ChangeOutboxResult]:
        return self.outbox.publish_ready(self.mailbox)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            started_at = time.monotonic()
            active_wake: ChangeIntent | None = None
            try:
                results = self.run_once()
                if self.wake_callback is not None:
                    for result in results:
                        if result.status == "delivered" and result.intent is not None:
                            self._pending_wakes.setdefault(
                                result.intent.message_id,
                                result.intent,
                            )
                    for message_id, intent in tuple(self._pending_wakes.items()):
                        active_wake = intent
                        await self.wake_callback(intent)
                        self._pending_wakes.pop(message_id, None)
                        active_wake = None
            except _ChangeOutboxIterationError as exc:
                self.outbox._log(
                    "change_outbox.forwarder_error",
                    "retrying",
                    exc.intent,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                    error_code=exc.error_code,
                )
            except Exception as exc:
                if active_wake is not None:
                    self.outbox._log(
                        "change_outbox.forwarder_error",
                        "retrying",
                        active_wake,
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                        error_code=type(exc).__name__,
                    )
                else:
                    self.outbox._facade.info_event(
                        "change_outbox.forwarder_error",
                        "retrying",
                        project_id=self.outbox.project_id,
                        duration_ms=round((time.monotonic() - started_at) * 1000, 3),
                        error_code=type(exc).__name__,
                        detail={"retry_in_seconds": self.poll_seconds},
                    )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue


def file_digest(path: Path) -> str:
    if not path.is_file():
        return missing_digest()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def missing_digest() -> str:
    return _MISSING_DIGEST


def formal_write_entry_inventory() -> dict[str, Any]:
    from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor

    class_source = inspect.getsource(SandboxedToolExecutor)
    tree = ast.parse(textwrap.dedent(class_source))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    mutation_calls = {"replace", "rename", "unlink", "write_bytes", "write_text"}
    direct_mutation_methods: list[str] = []
    boundary_callers: list[str] = []
    scanned_method_count = 0
    for method in class_node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scanned_method_count += 1
        calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]
        called_attributes = {
            call.func.attr for call in calls if isinstance(call.func, ast.Attribute)
        }
        if called_attributes & mutation_calls or "commit" in called_attributes:
            direct_mutation_methods.append(method.name)
        if "_apply_patch_to_workspace" in called_attributes:
            boundary_callers.append(method.name)

    source = inspect.getsource(SandboxedToolExecutor._apply_patch_to_workspace)
    return {
        "formal_entries": [
            f"SandboxedToolExecutor.{name}" for name in sorted(direct_mutation_methods)
        ],
        "required_committer": "AtomicPatchCommitter",
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "direct_mutation_methods": sorted(direct_mutation_methods),
        "boundary_callers": sorted(boundary_callers),
        "scanned_method_count": scanned_method_count,
        "excluded_categories": ["candidate_container", "diagnostics", "map_metadata", ".bridle"],
    }


def _text_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _utc_text(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _duration_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text
