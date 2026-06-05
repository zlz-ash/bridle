"""Server-sent events API."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from bridle.events.bus import EventBus

router = APIRouter(tags=["events"])

_KEEPALIVE_SECONDS = 15


async def _event_stream(
    *,
    last_seq: int | None,
    types: set[str] | None,
) -> AsyncIterator[dict]:
    yield {"comment": "connected"}
    async for event in EventBus.instance().subscribe(last_seq=last_seq, types=types):
        yield {
            "id": str(event.seq),
            "event": event.type,
            "data": json.dumps(event.payload, ensure_ascii=False, default=str),
        }


@router.get("/events")
async def stream_events(
    request: Request,
    types: str | None = Query(default=None),
) -> EventSourceResponse:
    last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
    last_seq: int | None = None
    if last_event_id:
        try:
            last_seq = int(last_event_id)
        except ValueError:
            last_seq = None

    type_filter: set[str] | None = None
    if types:
        type_filter = {part.strip() for part in types.split(",") if part.strip()}

    return EventSourceResponse(
        _event_stream(last_seq=last_seq, types=type_filter),
        ping=_KEEPALIVE_SECONDS,
        media_type="text/event-stream",
    )
