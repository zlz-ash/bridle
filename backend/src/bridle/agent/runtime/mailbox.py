"""Canonical mail values and loop-owned wakeup primitives."""
from __future__ import annotations

import asyncio
import json
import re
import threading
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote_to_bytes

_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class MailboxError(RuntimeError):
    """Stable mailbox error without storage or payload details."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _encode_component(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise MailboxError("invalid_address")
    return quote(value, safe="", encoding="utf-8", errors="strict")


def _decode_component(value: str) -> str:
    if not value or _PERCENT_ESCAPE.search(value):
        raise MailboxError("invalid_address")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise MailboxError("invalid_address") from exc
    if not decoded or _encode_component(decoded) != value:
        raise MailboxError("invalid_address")
    return decoded


@dataclass(frozen=True)
class AgentAddress:
    """Canonical project-scoped runtime address."""

    project_id: str
    agent_id: str
    generation: int

    def __post_init__(self) -> None:
        _encode_component(self.project_id)
        _encode_component(self.agent_id)
        if not isinstance(self.generation, int) or isinstance(self.generation, bool):
            raise MailboxError("invalid_address")
        if self.generation < 1:
            raise MailboxError("invalid_address")

    def to_uri(self) -> str:
        return (
            f"agent://{_encode_component(self.project_id)}/"
            f"{_encode_component(self.agent_id)}/{self.generation}"
        )

    @classmethod
    def parse(cls, value: str) -> AgentAddress:
        if not isinstance(value, str) or not value.startswith("agent://"):
            raise MailboxError("invalid_address")
        parts = value.removeprefix("agent://").split("/")
        if len(parts) != 3 or not parts[2].isdigit():
            raise MailboxError("invalid_address")
        try:
            generation = int(parts[2])
        except ValueError as exc:
            raise MailboxError("invalid_address") from exc
        address = cls(
            project_id=_decode_component(parts[0]),
            agent_id=_decode_component(parts[1]),
            generation=generation,
        )
        if address.to_uri() != value:
            raise MailboxError("invalid_address")
        return address

    def __str__(self) -> str:
        return self.to_uri()


@dataclass(frozen=True)
class MailEnvelope:
    """Immutable application payload with delivery metadata kept outside JSON."""

    message_id: str
    message_type: str
    source: AgentAddress
    target: AgentAddress
    payload: Any
    sequence_no: int | None = None
    attempt: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    payload_json: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str) or not self.message_id:
            raise MailboxError("invalid_envelope")
        if not isinstance(self.message_type, str) or not self.message_type:
            raise MailboxError("invalid_envelope")
        if not isinstance(self.source, AgentAddress) or not isinstance(self.target, AgentAddress):
            raise MailboxError("invalid_address")
        if self.sequence_no is not None and self.sequence_no < 1:
            raise MailboxError("invalid_envelope")
        if self.attempt < 0:
            raise MailboxError("invalid_envelope")
        if self.created_at is not None:
            object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))
        try:
            encoded = json.dumps(
                self.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            detached = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MailboxError("invalid_payload") from exc
        object.__setattr__(self, "payload", detached)
        object.__setattr__(self, "payload_json", encoded)

    @classmethod
    def from_storage(
        cls,
        *,
        message_id: str,
        message_type: str,
        source_address: str,
        target_address: str,
        payload_json: str,
        sequence_no: int,
        attempt: int,
        created_at: str,
        updated_at: str,
    ) -> MailEnvelope:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise MailboxError("invalid_payload") from exc
        return cls(
            message_id=message_id,
            message_type=message_type,
            source=AgentAddress.parse(source_address),
            target=AgentAddress.parse(target_address),
            payload=payload,
            sequence_no=sequence_no,
            attempt=attempt,
            created_at=parse_utc(created_at),
            updated_at=parse_utc(updated_at),
        )


@dataclass(frozen=True)
class MailboxResult:
    status: str
    message_id: str | None = None
    sequence_no: int | None = None
    attempt: int = 0
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_retry_at: datetime | None = None
    envelope: MailEnvelope | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_text(value: datetime) -> str:
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise MailboxError("invalid_timestamp") from exc
    return ensure_utc(parsed)


class MailboxWakeSignal:
    """One mailbox instance's event-loop-owned wake state."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._version = 0
        self._pending_notification = False
        self._closed = False
        self._waiters: set[asyncio.Future[bool]] = set()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def waiter_count(self) -> int:
        with self._lock:
            return len(self._waiters)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def bind_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        advance_pending = False
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                raise MailboxError("mailbox_loop_mismatch")
            self._loop = loop
            if self._pending_notification:
                self._pending_notification = False
                advance_pending = True
        if advance_pending:
            self._advance_on_loop()

    def notify(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            loop = self._loop
            if loop is None:
                self._pending_notification = True
                return True
            if loop.is_closed():
                return False
        try:
            loop.call_soon_threadsafe(self._advance_on_loop)
        except RuntimeError:
            return False
        return True

    def _advance_on_loop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._version += 1
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(True)

    async def wait_for_change(self, version: int, timeout: float | None) -> str:
        self.bind_running_loop()
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                return "closed"
            if self._version != version:
                return "changed"
            waiter: asyncio.Future[bool] = loop.create_future()
            self._waiters.add(waiter)
        try:
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout=timeout)
            return "closed" if self.closed else "changed"
        except TimeoutError:
            return "timeout"
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    def close_on_loop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._version += 1
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(False)


_SIGNALS_LOCK = threading.RLock()
_SIGNALS: dict[Path, weakref.WeakSet[MailboxWakeSignal]] = {}


def register_wake_signal(database_path: Path) -> MailboxWakeSignal:
    path = database_path.resolve()
    signal = MailboxWakeSignal(path)
    with _SIGNALS_LOCK:
        signals = _SIGNALS.setdefault(path, weakref.WeakSet())
        signals.add(signal)
    return signal


def unregister_wake_signal(signal: MailboxWakeSignal) -> None:
    with _SIGNALS_LOCK:
        signals = _SIGNALS.get(signal.database_path)
        if signals is None:
            return
        signals.discard(signal)
        if not signals:
            _SIGNALS.pop(signal.database_path, None)


def notify_database(database_path: Path) -> int:
    path = database_path.resolve()
    with _SIGNALS_LOCK:
        signals = tuple(_SIGNALS.get(path, ()))
    return sum(signal.notify() for signal in signals)
