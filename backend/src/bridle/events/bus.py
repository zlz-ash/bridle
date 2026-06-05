"""In-process event bus for SSE subscribers."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bridle.logging.jsonl import log_event

logger = logging.getLogger("bridle.events")

RING_BUFFER_SIZE = 200
SUBSCRIBER_QUEUE_SIZE = 100


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    type: str
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBus:
    _instance: EventBus | None = None

    def __init__(self) -> None:
        self._seq = 0
        self._ring: deque[Event] = deque(maxlen=RING_BUFFER_SIZE)
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> EventBus:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_instance(cls) -> None:
        """Replace the process-wide singleton (app startup and isolated tests)."""
        cls._instance = cls()

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._reset_instance()

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event_type: str, payload: dict[str, Any]) -> Event:
        self._seq += 1
        event = Event(
            seq=self._seq,
            type=event_type,
            occurred_at=datetime.now(UTC).isoformat(),
            payload=payload,
        )
        self._ring.append(event)
        for queue in list(self._subscribers):
            self._deliver(queue, event)
        return event

    def _deliver(self, queue: asyncio.Queue[Event | None], event: Event) -> None:
        if queue.full():
            try:
                queue.get_nowait()
                log_event(
                    "event_subscriber_dropped",
                    "rejected",
                    detail={"seq": event.seq, "type": event.type},
                )
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            log_event(
                "event_subscriber_dropped",
                "rejected",
                detail={"seq": event.seq, "type": event.type, "reason": "queue_full"},
            )

    async def subscribe(
        self,
        *,
        last_seq: int | None = None,
        types: set[str] | None = None,
    ) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            for event in self._ring:
                if last_seq is not None and event.seq <= last_seq:
                    continue
                if types and event.type not in types:
                    continue
                yield event
            while True:
                item = await queue.get()
                if item is None:
                    break
                if types and item.type not in types:
                    continue
                yield item
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


def publish_event_safe(event_type: str, payload: dict[str, Any]) -> None:
    try:
        EventBus.instance().publish(event_type, payload)
    except Exception as exc:
        logger.warning(
            "event_publish_failed",
            extra={
                "action": "event_publish_failed",
                "status": "failed",
                "detail": {"type": event_type, "error": type(exc).__name__},
            },
        )
