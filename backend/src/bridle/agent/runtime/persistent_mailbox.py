"""Project-local persistent mailbox backed by one isolated SQLite database."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bridle.agent.runtime.mailbox import (
    AgentAddress,
    MailboxError,
    MailboxResult,
    MailEnvelope,
    ensure_utc,
    notify_database,
    parse_utc,
    register_wake_signal,
    unregister_wake_signal,
    utc_now,
    utc_text,
)
from bridle.agent.runtime.project_storage import initialize_project_store
from bridle.logging.facade import LoggingFacade, get_logging_facade
from bridle.observability.context import current_log_context

Clock = Callable[[], datetime]
EmptyWaitHook = Callable[[], Awaitable[None]]


class PersistentMailbox:
    """Durable ordered delivery with renewable fenced leases."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        project_id: str,
        consumer_id: str,
        capacity: int = 1000,
        lease_seconds: float = 30.0,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
        busy_timeout_ms: int = 100,
        clock: Clock | None = None,
        facade: LoggingFacade | None = None,
        empty_wait_hook: EmptyWaitHook | None = None,
        default_target: AgentAddress | None = None,
        trace_id: str | None = None,
    ) -> None:
        if capacity < 1:
            raise MailboxError("invalid_capacity")
        if lease_seconds <= 0 or retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise MailboxError("invalid_mailbox_timing")
        if not project_id or not consumer_id:
            raise MailboxError("invalid_mailbox_identity")
        if default_target is not None and default_target.project_id != project_id:
            raise MailboxError("invalid_address")
        self.database_path = Path(database_path).resolve()
        self.project_id = project_id
        self.consumer_id = consumer_id
        self.capacity = capacity
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.busy_timeout_ms = max(0, busy_timeout_ms)
        self._clock = clock or utc_now
        self._logging = facade or get_logging_facade()
        context_trace = current_log_context().get("trace_id")
        self.trace_id = trace_id or (
            str(context_trace) if context_trace else f"mailbox-{secrets.token_hex(8)}"
        )
        self._empty_wait_hook = empty_wait_hook
        self._default_target = default_target
        self._signal = register_wake_signal(self.database_path)
        self._closed = False
        initialize_project_store(
            self.database_path,
            store_kind="mail",
            project_id=self.project_id,
            facade=self._logging,
        )

    @property
    def wake_version(self) -> int:
        return self._signal.version

    @property
    def waiter_count(self) -> int:
        return self._signal.waiter_count

    def has_pending(self, target: AgentAddress) -> bool:
        """Return whether durable, non-delivered mail exists for one target."""
        self._validate_target(target)
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT 1 FROM mail_messages "
                    "WHERE target_address = ? AND status != 'delivered' LIMIT 1",
                    (target.to_uri(),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        return row is not None

    def is_empty_at_version(self, target: AgentAddress, expected_version: int) -> bool:
        """Fence an idle check with the process-local database wake version."""
        self._validate_target(target)
        if self.wake_version != expected_version:
            return False
        empty = not self.has_pending(target)
        return empty and self.wake_version == expected_version

    def _now(self) -> datetime:
        return ensure_utc(self._clock())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _is_busy(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "locked" in message or "busy" in message

    def _target_for_log(self, target: AgentAddress | None) -> AgentAddress:
        if target is not None:
            return target
        if self._default_target is not None:
            return self._default_target
        return AgentAddress(self.project_id, self.consumer_id, 1)

    def _log(
        self,
        action: str,
        status: str,
        *,
        target: AgentAddress | None,
        message_id: str | None = None,
        sequence_no: int | None = None,
        attempt: int = 0,
        lease_token: str | None = None,
        duration_ms: int = 0,
        error_code: str | None = None,
        warning: bool = False,
    ) -> None:
        address = self._target_for_log(target)
        detail: dict[str, Any] = {
            "sequence_no": sequence_no,
            "attempt": attempt,
        }
        if lease_token is not None:
            detail["lease_token_digest"] = hashlib.sha256(lease_token.encode()).hexdigest()[:12]
        fields = {
            "trace_id": self.trace_id,
            "message_id": message_id,
            "project_id": self.project_id,
            "agent_id": address.agent_id,
            "generation": address.generation,
            "duration_ms": max(0, duration_ms),
            "error_code": error_code,
            "detail": detail,
        }
        if warning:
            self._logging.warn_event(action, status, **fields)
        else:
            self._logging.info_event(action, status, **fields)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.perf_counter() - started) * 1000))

    def _result(
        self,
        status: str,
        *,
        row: sqlite3.Row | None = None,
        lease_token: str | None = None,
        lease_expires_at: datetime | None = None,
        next_retry_at: datetime | None = None,
    ) -> MailboxResult:
        envelope = None
        if row is not None:
            envelope = MailEnvelope.from_storage(
                message_id=str(row["message_id"]),
                message_type=str(row["message_type"]),
                source_address=str(row["source_address"]),
                target_address=str(row["target_address"]),
                payload_json=str(row["payload_json"]),
                sequence_no=int(row["sequence_no"]),
                attempt=int(row["attempt"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        return MailboxResult(
            status=status,
            message_id=str(row["message_id"]) if row is not None else None,
            sequence_no=int(row["sequence_no"]) if row is not None else None,
            attempt=int(row["attempt"]) if row is not None else 0,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            next_retry_at=next_retry_at,
            envelope=envelope,
        )

    def _busy_result(
        self,
        *,
        target: AgentAddress | None,
        message_id: str | None,
        started: float,
    ) -> MailboxResult:
        self._log(
            "mail.busy",
            "retryable",
            target=target,
            message_id=message_id,
            duration_ms=self._elapsed_ms(started),
            error_code="mailbox_busy",
            warning=True,
        )
        return MailboxResult(status="mailbox_busy")

    def _validate_target(self, target: AgentAddress) -> None:
        if target.project_id != self.project_id:
            raise MailboxError("invalid_address")

    def enqueue(self, envelope: MailEnvelope) -> MailboxResult:
        started = time.perf_counter()
        if self._closed:
            return MailboxResult(status="closed")
        self._validate_target(envelope.source)
        self._validate_target(envelope.target)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM mail_messages WHERE message_id = ?",
                    (envelope.message_id,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    self._log(
                        "mail.enqueued",
                        "existing",
                        target=envelope.target,
                        message_id=envelope.message_id,
                        sequence_no=int(existing["sequence_no"]),
                        attempt=int(existing["attempt"]),
                        duration_ms=self._elapsed_ms(started),
                    )
                    return self._result("existing", row=existing)
                active_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM mail_messages WHERE status != 'delivered'"
                    ).fetchone()[0]
                )
                if active_count >= self.capacity:
                    connection.rollback()
                    self._log(
                        "mail.backpressure",
                        "rejected",
                        target=envelope.target,
                        message_id=envelope.message_id,
                        duration_ms=self._elapsed_ms(started),
                        error_code="mailbox_capacity",
                        warning=True,
                    )
                    return MailboxResult(
                        status="backpressure",
                        message_id=envelope.message_id,
                    )
                sequence_no = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM mail_messages"
                    ).fetchone()[0]
                )
                now_text = utc_text(self._now())
                connection.execute(
                    """
                    INSERT INTO mail_messages(
                        message_id, source_address, target_address, message_type,
                        payload_json, sequence_no, status, attempt, next_retry_at,
                        lease_owner, lease_token, lease_expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        envelope.message_id,
                        envelope.source.to_uri(),
                        envelope.target.to_uri(),
                        envelope.message_type,
                        envelope.payload_json,
                        sequence_no,
                        now_text,
                        now_text,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM mail_messages WHERE message_id = ?", (envelope.message_id,)
                ).fetchone()
                connection.commit()
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                return self._busy_result(
                    target=envelope.target,
                    message_id=envelope.message_id,
                    started=started,
                )
            raise MailboxError("mailbox_storage_error") from exc
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        assert row is not None
        self._log(
            "mail.enqueued",
            "completed",
            target=envelope.target,
            message_id=envelope.message_id,
            sequence_no=sequence_no,
            duration_ms=self._elapsed_ms(started),
        )
        self.notify(
            target=envelope.target,
            message_id=envelope.message_id,
            sequence_no=sequence_no,
        )
        return self._result("inserted", row=row)

    def claim(self, target: AgentAddress) -> MailboxResult:
        started = time.perf_counter()
        self._validate_target(target)
        if self._closed:
            return MailboxResult(status="closed")
        now = self._now()
        now_text = utc_text(now)
        token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=self.lease_seconds)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT * FROM mail_messages
                    WHERE target_address = ? AND (
                        status = 'pending'
                        OR (status = 'retry_wait' AND next_retry_at <= ?)
                        OR (status = 'leased' AND lease_expires_at <= ?)
                    )
                    ORDER BY sequence_no
                    LIMIT 1
                    """,
                    (target.to_uri(), now_text, now_text),
                ).fetchone()
                if row is None:
                    connection.commit()
                    self._log(
                        "mail.empty",
                        "empty",
                        target=target,
                        duration_ms=self._elapsed_ms(started),
                    )
                    return MailboxResult(status="empty")
                connection.execute(
                    """
                    UPDATE mail_messages
                    SET status='leased', attempt=attempt+1, next_retry_at=NULL,
                        lease_owner=?, lease_token=?, lease_expires_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (self.consumer_id, token, utc_text(expires_at), now_text, int(row["id"])),
                )
                claimed = connection.execute(
                    "SELECT * FROM mail_messages WHERE id=?", (int(row["id"]),)
                ).fetchone()
                connection.commit()
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                return self._busy_result(target=target, message_id=None, started=started)
            raise MailboxError("mailbox_storage_error") from exc
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        assert claimed is not None
        self._log(
            "mail.claimed",
            "completed",
            target=target,
            message_id=str(claimed["message_id"]),
            sequence_no=int(claimed["sequence_no"]),
            attempt=int(claimed["attempt"]),
            lease_token=token,
            duration_ms=self._elapsed_ms(started),
        )
        return self._result(
            "claimed",
            row=claimed,
            lease_token=token,
            lease_expires_at=expires_at,
        )

    def _leased_row(
        self,
        connection: sqlite3.Connection,
        *,
        message_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM mail_messages WHERE message_id = ?", (message_id,)
        ).fetchone()

    def _lease_matches(
        self,
        row: sqlite3.Row | None,
        *,
        lease_token: str,
        target: AgentAddress,
        now: datetime,
    ) -> bool:
        return bool(
            row is not None
            and row["status"] == "leased"
            and row["lease_owner"] == self.consumer_id
            and row["lease_token"] == lease_token
            and row["target_address"] == target.to_uri()
            and row["lease_expires_at"] is not None
            and parse_utc(str(row["lease_expires_at"])) > now
        )

    def _lost_lease(
        self,
        *,
        row: sqlite3.Row | None,
        message_id: str,
        lease_token: str,
        target: AgentAddress,
        started: float,
    ) -> MailboxResult:
        self._log(
            "mail.lease_lost",
            "rejected",
            target=target,
            message_id=message_id,
            sequence_no=int(row["sequence_no"]) if row is not None else None,
            attempt=int(row["attempt"]) if row is not None else 0,
            lease_token=lease_token,
            duration_ms=self._elapsed_ms(started),
            error_code="lost_lease",
            warning=True,
        )
        return self._result("lost_lease", row=row)

    def renew(
        self,
        message_id: str,
        lease_token: str,
        *,
        target: AgentAddress,
    ) -> MailboxResult:
        started = time.perf_counter()
        self._validate_target(target)
        now = self._now()
        expires_at = now + timedelta(seconds=self.lease_seconds)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._leased_row(connection, message_id=message_id)
                if not self._lease_matches(
                    row, lease_token=lease_token, target=target, now=now
                ):
                    connection.rollback()
                    return self._lost_lease(
                        row=row,
                        message_id=message_id,
                        lease_token=lease_token,
                        target=target,
                        started=started,
                    )
                connection.execute(
                    "UPDATE mail_messages SET lease_expires_at=?, updated_at=? WHERE id=?",
                    (utc_text(expires_at), utc_text(now), int(row["id"])),
                )
                renewed = self._leased_row(connection, message_id=message_id)
                connection.commit()
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                return self._busy_result(target=target, message_id=message_id, started=started)
            raise MailboxError("mailbox_storage_error") from exc
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        assert renewed is not None
        self._log(
            "mail.renewed",
            "completed",
            target=target,
            message_id=message_id,
            sequence_no=int(renewed["sequence_no"]),
            attempt=int(renewed["attempt"]),
            lease_token=lease_token,
            duration_ms=self._elapsed_ms(started),
        )
        return self._result(
            "renewed",
            row=renewed,
            lease_token=lease_token,
            lease_expires_at=expires_at,
        )

    def ack(
        self,
        message_id: str,
        lease_token: str,
        *,
        target: AgentAddress,
    ) -> MailboxResult:
        started = time.perf_counter()
        self._validate_target(target)
        now = self._now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._leased_row(connection, message_id=message_id)
                if not self._lease_matches(
                    row, lease_token=lease_token, target=target, now=now
                ):
                    connection.rollback()
                    return self._lost_lease(
                        row=row,
                        message_id=message_id,
                        lease_token=lease_token,
                        target=target,
                        started=started,
                    )
                connection.execute(
                    """
                    UPDATE mail_messages
                    SET status='delivered', next_retry_at=NULL, lease_owner=NULL,
                        lease_token=NULL, lease_expires_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (utc_text(now), int(row["id"])),
                )
                delivered = self._leased_row(connection, message_id=message_id)
                connection.commit()
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                return self._busy_result(target=target, message_id=message_id, started=started)
            raise MailboxError("mailbox_storage_error") from exc
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        assert delivered is not None
        self._log(
            "mail.acked",
            "completed",
            target=target,
            message_id=message_id,
            sequence_no=int(delivered["sequence_no"]),
            attempt=int(delivered["attempt"]),
            lease_token=lease_token,
            duration_ms=self._elapsed_ms(started),
        )
        return self._result("acked", row=delivered)

    def nack(
        self,
        message_id: str,
        lease_token: str,
        *,
        target: AgentAddress,
    ) -> MailboxResult:
        started = time.perf_counter()
        self._validate_target(target)
        now = self._now()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._leased_row(connection, message_id=message_id)
                if not self._lease_matches(
                    row, lease_token=lease_token, target=target, now=now
                ):
                    connection.rollback()
                    return self._lost_lease(
                        row=row,
                        message_id=message_id,
                        lease_token=lease_token,
                        target=target,
                        started=started,
                    )
                delay = min(
                    self.retry_base_seconds * (2 ** max(0, int(row["attempt"]) - 1)),
                    self.retry_max_seconds,
                )
                next_retry_at = now + timedelta(seconds=delay)
                connection.execute(
                    """
                    UPDATE mail_messages
                    SET status='retry_wait', next_retry_at=?, lease_owner=NULL,
                        lease_token=NULL, lease_expires_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (utc_text(next_retry_at), utc_text(now), int(row["id"])),
                )
                retrying = self._leased_row(connection, message_id=message_id)
                connection.commit()
        except sqlite3.OperationalError as exc:
            if self._is_busy(exc):
                return self._busy_result(target=target, message_id=message_id, started=started)
            raise MailboxError("mailbox_storage_error") from exc
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        assert retrying is not None
        for action in ("mail.nacked", "mail.retry_scheduled"):
            self._log(
                action,
                "completed",
                target=target,
                message_id=message_id,
                sequence_no=int(retrying["sequence_no"]),
                attempt=int(retrying["attempt"]),
                lease_token=lease_token,
                duration_ms=self._elapsed_ms(started),
            )
        self.notify(
            target=target,
            message_id=message_id,
            sequence_no=int(retrying["sequence_no"]),
            attempt=int(retrying["attempt"]),
        )
        return self._result("nacked", row=retrying, next_retry_at=next_retry_at)

    def notify(
        self,
        *,
        target: AgentAddress | None = None,
        message_id: str | None = None,
        sequence_no: int | None = None,
        attempt: int = 0,
    ) -> None:
        started = time.perf_counter()
        notified = notify_database(self.database_path)
        if notified:
            self._log(
                "mail.wakeup",
                "completed",
                target=target,
                message_id=message_id,
                sequence_no=sequence_no,
                attempt=attempt,
                duration_ms=self._elapsed_ms(started),
            )

    def _next_due_delay(self, target: AgentAddress) -> float | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT MIN(
                        CASE status
                            WHEN 'retry_wait' THEN next_retry_at
                            WHEN 'leased' THEN lease_expires_at
                        END
                    )
                    FROM mail_messages
                    WHERE target_address = ? AND status IN ('retry_wait', 'leased')
                    """,
                    (target.to_uri(),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        if row is None or row[0] is None:
            return None
        return max(0.0, (parse_utc(str(row[0])) - self._now()).total_seconds())

    async def receive(
        self,
        target: AgentAddress | None = None,
        *,
        timeout: float | None = None,
    ) -> MailboxResult:
        selected_target = target or self._default_target
        if selected_target is None:
            raise MailboxError("missing_target")
        self._validate_target(selected_target)
        self._signal.bind_running_loop()
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            if self._closed:
                return MailboxResult(status="closed")
            claimed = self.claim(selected_target)
            if claimed.status != "empty":
                return claimed
            version = self._signal.version
            if self._empty_wait_hook is not None:
                await self._empty_wait_hook()
            if self._closed:
                return MailboxResult(status="closed")
            rechecked = self.claim(selected_target)
            if rechecked.status != "empty":
                return rechecked
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            if remaining == 0:
                return rechecked
            due_delay = self._next_due_delay(selected_target)
            wait_timeout = remaining
            if due_delay is not None:
                wait_timeout = due_delay if wait_timeout is None else min(wait_timeout, due_delay)
            if wait_timeout == 0:
                continue
            wake_status = await self._signal.wait_for_change(version, wait_timeout)
            if wake_status == "closed":
                return MailboxResult(status="closed")
            if wake_status == "timeout":
                if deadline is not None and loop.time() >= deadline:
                    return MailboxResult(status="empty")
                continue

    async def close(self) -> MailboxResult:
        if self._closed:
            return MailboxResult(status="closed")
        started = time.perf_counter()
        released = 0
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                now_text = utc_text(self._now())
                cursor = connection.execute(
                    """
                    UPDATE mail_messages
                    SET status='pending', next_retry_at=NULL, lease_owner=NULL,
                        lease_token=NULL, lease_expires_at=NULL, updated_at=?
                    WHERE status='leased' AND lease_owner=?
                    """,
                    (now_text, self.consumer_id),
                )
                released = max(0, int(cursor.rowcount))
                connection.commit()
        except sqlite3.OperationalError as exc:
            if not self._is_busy(exc):
                raise MailboxError("mailbox_storage_error") from exc
            return self._busy_result(
                target=self._default_target,
                message_id=None,
                started=started,
            )
        except sqlite3.Error as exc:
            raise MailboxError("mailbox_storage_error") from exc
        self._closed = True
        self._signal.bind_running_loop()
        self._signal.close_on_loop()
        unregister_wake_signal(self._signal)
        self._log(
            "mail.closed",
            "completed",
            target=self._default_target,
            attempt=released,
            duration_ms=self._elapsed_ms(started),
        )
        return MailboxResult(status="closed")
