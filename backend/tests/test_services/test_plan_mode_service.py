"""PlanModeService unit tests."""
from __future__ import annotations

import json
from typing import Any

import pytest

from bridle.schemas.plan_mode import ChatTurnSchema
from bridle.engine.openai_client import LLMHttpError
from bridle.services.plan_mode_service import (
    PlanModeService,
    PlannerTimeoutError,
    _parse_planner_reply,
)
from tests.helpers.plan_factory import two_node_plan


class FakePlannerClient:
    def __init__(self, *, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    async def chat_completion(
        self,
        *,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append({"messages": messages, "model": model, "timeout_seconds": timeout_seconds})
        if self._error:
            raise self._error
        assert self._response is not None
        return self._response


def _assistant_content(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


@pytest.mark.asyncio
async def test_converse_parses_valid_plan_fence() -> None:
    plan = two_node_plan()
    body = f"Here is your plan.\n```json\n{json.dumps(plan)}\n```"
    client = FakePlannerClient(response=_assistant_content(body))
    result = await PlanModeService.converse([], {}, client=client)
    assert result.reply == "Here is your plan."
    assert result.proposed_plan is not None
    assert result.proposed_plan.goal == plan["goal"]
    assert result.parse_error is None


@pytest.mark.asyncio
async def test_converse_broken_json_sets_parse_error() -> None:
    body = "Draft ready.\n```json\n{not json}\n```"
    client = FakePlannerClient(response=_assistant_content(body))
    result = await PlanModeService.converse([], {}, client=client)
    assert result.proposed_plan is None
    assert result.parse_error


@pytest.mark.asyncio
async def test_converse_timeout_from_llm_http() -> None:
    client = FakePlannerClient(error=LLMHttpError(408, "timeout"))
    with pytest.raises(PlannerTimeoutError):
        await PlanModeService.converse([], {}, client=client)


@pytest.mark.asyncio
async def test_converse_handles_non_ascii_history() -> None:
    client = FakePlannerClient(response=_assistant_content("你好"))
    result = await PlanModeService.converse(
        [ChatTurnSchema(role="user", content="中文需求")],
        {},
        client=client,
    )
    assert "你好" in result.reply


@pytest.mark.asyncio
async def test_converse_empty_content_history() -> None:
    client = FakePlannerClient(response=_assistant_content("ok"))
    result = await PlanModeService.converse(
        [ChatTurnSchema(role="user", content="")],
        {},
        client=client,
    )
    assert result.reply == "ok"


def test_parse_planner_reply_uses_last_fence() -> None:
    plan = two_node_plan()
    text = f"Here is the final plan:\n```json\n{json.dumps(plan)}\n```"
    reply, proposed, err = _parse_planner_reply(text)
    assert err is None
    assert proposed is not None
    assert proposed.goal == plan["goal"]
    assert reply == "Here is the final plan:"


@pytest.mark.asyncio
async def test_converse_plan_in_middle_of_text() -> None:
    plan = two_node_plan()
    body = f"prefix ```json\n{json.dumps(plan)}\n``` suffix"
    client = FakePlannerClient(response=_assistant_content(body))
    result = await PlanModeService.converse([], {}, client=client)
    assert result.proposed_plan is not None
    assert "prefix" in result.reply
    assert "suffix" in result.reply
