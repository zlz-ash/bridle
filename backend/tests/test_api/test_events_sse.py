"""SSE integration tests."""
from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient

from tests.helpers.plan_factory import two_node_plan


async def _read_sse_event(response, *, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    buffer = ""
    text_iter = response.aiter_text()

    def _parse_buffer(raw: str) -> dict | None:
        normalized = raw.replace("\r\n", "\n")
        while "\n\n" in normalized:
            block, normalized = normalized.split("\n\n", 1)
            if not block.strip() or block.strip().startswith(":"):
                continue
            event_name = None
            event_id = None
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("id:"):
                    event_id = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = json.loads(line.split(":", 1)[1].strip())
                    return {"event": event_name, "id": event_id, "data": data}
        return None

    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(text_iter.__anext__(), timeout=remaining)
        except (StopAsyncIteration, asyncio.TimeoutError):
            break
        buffer += chunk
        parsed = _parse_buffer(buffer)
        if parsed is not None:
            return parsed

    parsed = _parse_buffer(buffer)
    if parsed is not None:
        return parsed
    raise TimeoutError(f"no sse event received; buffer={buffer[:500]!r}")


@pytest.mark.asyncio
async def test_chat_message_triggers_sse(
    live_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
    task_resp = await live_client.post("/api/v1/tasks", json={"title": "SSE chat"})
    plan_resp = await live_client.post(
        f"/api/v1/tasks/{task_resp.json()['id']}/plan/import",
        json=two_node_plan(),
    )
    session_id = (
        await live_client.post(
            "/api/v1/agent/coding-sessions",
            json={"plan_id": plan_resp.json()["plan_id"]},
        )
    ).json()["session_id"]

    create = await live_client.post(
        f"/api/v1/agent/coding-sessions/{session_id}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert create.status_code == 201

    async with live_client.stream("GET", "/api/v1/events") as response:
        event = await _read_sse_event(response, timeout=5.0)
        assert event["event"] == "chat_message_appended"
        assert event["data"]["session_id"] == session_id
        assert event["data"]["role"] == "user"


@pytest.mark.asyncio
async def test_types_filter_limits_events(
    live_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
    task_resp = await live_client.post("/api/v1/tasks", json={"title": "SSE filter"})
    plan_resp = await live_client.post(
        f"/api/v1/tasks/{task_resp.json()['id']}/plan/import",
        json=two_node_plan(),
    )
    session_id = (
        await live_client.post(
            "/api/v1/agent/coding-sessions",
            json={"plan_id": plan_resp.json()["plan_id"]},
        )
    ).json()["session_id"]

    await live_client.post(
        f"/api/v1/agent/coding-sessions/{session_id}/messages",
        json={"role": "assistant", "content": "ok"},
    )

    async with live_client.stream(
        "GET",
        "/api/v1/events?types=chat_message_appended",
    ) as response:
        event = await _read_sse_event(response, timeout=5.0)
        assert event["event"] == "chat_message_appended"
