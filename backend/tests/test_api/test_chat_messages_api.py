from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.helpers.plan_factory import two_node_plan


async def _create_session(client: AsyncClient) -> str:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Chat API Task"})
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["id"]
    plan_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
    assert plan_resp.status_code == 200, plan_resp.text
    session_resp = await client.post(
        "/api/v1/agent/coding-sessions",
        json={"plan_id": plan_resp.json()["plan_id"]},
    )
    assert session_resp.status_code == 200, session_resp.text
    return session_resp.json()["session_id"]


class TestChatMessagesAPI:
    async def test_create_and_list_chat_messages(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
        session_id = await _create_session(client)

        create_user = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            json={"role": "user", "content": "Please implement the node"},
        )
        create_tool = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            json={
                "role": "tool",
                "content": "tests passed",
                "tool_calls": [{"id": "tc1", "name": "run_allowed_tests"}],
                "tool_result": {"status": "completed", "exit_code": 0},
            },
        )

        assert create_user.status_code == 201, create_user.text
        assert create_tool.status_code == 201, create_tool.text

        response = await client.get(f"/api/v1/agent/coding-sessions/{session_id}/messages")

        assert response.status_code == 200, response.text
        messages = response.json()
        assert [m["role"] for m in messages] == ["user", "tool"]
        assert messages[0]["content"] == "Please implement the node"
        assert messages[1]["tool_calls"] == [{"id": "tc1", "name": "run_allowed_tests"}]
        assert messages[1]["tool_result"] == {"status": "completed", "exit_code": 0}

    async def test_list_messages_created_after_filters(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
        session_id = await _create_session(client)
        first = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            json={"role": "user", "content": "one"},
        )
        assert first.status_code == 201
        second = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            json={"role": "assistant", "content": "two"},
        )
        third = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            json={"role": "user", "content": "three"},
        )
        assert second.status_code == 201 and third.status_code == 201
        all_msgs = (await client.get(f"/api/v1/agent/coding-sessions/{session_id}/messages")).json()
        assert len(all_msgs) == 3
        cutoff = all_msgs[1]["created_at"]
        filtered = await client.get(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            params={"created_after": cutoff},
        )
        assert filtered.status_code == 200
        roles = [m["role"] for m in filtered.json()]
        assert roles == ["user"]

    async def test_list_messages_invalid_created_after_returns_422(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
        session_id = await _create_session(client)
        response = await client.get(
            f"/api/v1/agent/coding-sessions/{session_id}/messages",
            params={"created_after": "not-a-timestamp"},
        )
        assert response.status_code == 422

    async def test_create_message_for_missing_session_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        response = await client.post(
            "/api/v1/agent/coding-sessions/missing-session/messages",
            json={"role": "user", "content": "hello"},
        )

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
