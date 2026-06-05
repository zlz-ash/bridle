"""Plan Mode API integration tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from bridle.models.agent_coding_session import AgentCodingSessionRecord
from bridle.models.chat_message import ChatMessageRecord
from bridle.schemas.plan import PlanImportSchema
from bridle.schemas.plan_mode import PlanModeResponseSchema
from bridle.services.plan_mode_service import PlannerTimeoutError
from tests.helpers.plan_factory import two_node_plan


@pytest.fixture
def mock_converse():
    with patch("bridle.api.plan_mode.PlanModeService.converse", new_callable=AsyncMock) as mock:
        yield mock


@pytest.mark.asyncio
async def test_plan_mode_converse_returns_parsed_plan(client: AsyncClient, mock_converse: AsyncMock) -> None:
    plan = two_node_plan()
    mock_converse.return_value = PlanModeResponseSchema(
        reply="Sounds good.",
        proposed_plan=PlanImportSchema(**plan),
        parse_error=None,
        raw_finish_reason="stop",
    )
    response = await client.post(
        "/api/v1/plan-mode/converse",
        json={"history": [{"role": "user", "content": "build add()"}], "workspace_overview": {"files": []}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Sounds good."
    assert body["proposed_plan"]["goal"] == plan["goal"]
    assert "```" not in body["reply"]


@pytest.mark.asyncio
async def test_plan_mode_converse_parse_error(client: AsyncClient, mock_converse: AsyncMock) -> None:
    mock_converse.return_value = PlanModeResponseSchema(
        reply="bad plan",
        proposed_plan=None,
        parse_error="invalid json",
    )
    response = await client.post(
        "/api/v1/plan-mode/converse",
        json={"history": [], "workspace_overview": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["proposed_plan"] is None
    assert body["parse_error"] == "invalid json"


@pytest.mark.asyncio
async def test_plan_mode_converse_timeout_504(client: AsyncClient, mock_converse: AsyncMock) -> None:
    mock_converse.side_effect = PlannerTimeoutError("timeout")
    response = await client.post(
        "/api/v1/plan-mode/converse",
        json={"history": [], "workspace_overview": {}},
    )
    assert response.status_code == 504
    assert response.json()["code"] == "planner_timeout"


@pytest.mark.asyncio
async def test_plan_mode_converse_long_history(client: AsyncClient, mock_converse: AsyncMock) -> None:
    mock_converse.return_value = PlanModeResponseSchema(reply="ok")
    history = [{"role": "user", "content": f"msg {i}"} for i in range(60)]
    response = await client.post(
        "/api/v1/plan-mode/converse",
        json={"history": history, "workspace_overview": {}},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_converse_does_not_touch_session_tables(
    client: AsyncClient, db, mock_converse: AsyncMock,
) -> None:
    from sqlalchemy import text

    mock_converse.return_value = PlanModeResponseSchema(reply="ok")
    response = await client.post(
        "/api/v1/plan-mode/converse",
        json={"history": [{"role": "user", "content": "hi"}], "workspace_overview": {}},
    )
    assert response.status_code == 200
    chat_count = await db.scalar(text("SELECT COUNT(*) FROM chat_messages"))
    session_count = await db.scalar(text("SELECT COUNT(*) FROM agent_coding_sessions"))
    assert chat_count == 0
    assert session_count == 0


@pytest.mark.asyncio
async def test_plan_mode_converse_does_not_write_db(client: AsyncClient, db, mock_converse: AsyncMock) -> None:
    mock_converse.return_value = PlanModeResponseSchema(reply="ok")
    before_sessions = await db.scalar(select(func.count()).select_from(AgentCodingSessionRecord))
    before_messages = await db.scalar(select(func.count()).select_from(ChatMessageRecord))
    response = await client.post(
        "/api/v1/plan-mode/converse",
        json={"history": [{"role": "user", "content": "hi"}], "workspace_overview": {}},
    )
    assert response.status_code == 200
    after_sessions = await db.scalar(select(func.count()).select_from(AgentCodingSessionRecord))
    after_messages = await db.scalar(select(func.count()).select_from(ChatMessageRecord))
    assert before_sessions == after_sessions == 0
    assert before_messages == after_messages == 0
