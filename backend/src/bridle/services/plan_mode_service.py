"""Plan Mode converse — stateless planner chat, no DB writes."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from bridle.engine.agent_provider import AgentProviderFactory
from bridle.engine.deepseek_client import DEEPSEEK_DEFAULT_BASE
from bridle.engine.openai_client import HttpOpenAICompatibleClient, LLMHttpError, OpenAICompatibleClient
from bridle.engine.planner_template import build_planner_messages
from bridle.logging.jsonl import log_event
from bridle.schemas.plan import PlanImportSchema
from bridle.schemas.plan_mode import ChatTurnSchema, PlanModeResponseSchema

logger = logging.getLogger("bridle")

_JSON_FENCE_RE = re.compile(r"```json\s*([\s\S]*?)\s*```", re.IGNORECASE)


class PlannerTimeoutError(Exception):
    pass


class PlannerAuthError(Exception):
    pass


class PlannerProviderError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _default_planner_client() -> OpenAICompatibleClient:
    cfg = AgentProviderFactory.get_config()
    api_key = cfg["api_key"]
    if not api_key:
        raise PlannerAuthError("Missing BRIDLE_AGENT_API_KEY for planner")
    base_url = cfg["base_url"] or DEEPSEEK_DEFAULT_BASE
    return HttpOpenAICompatibleClient(api_key=api_key, base_url=base_url, proxy=cfg["proxy"])


def _extract_content(response: dict[str, Any]) -> tuple[str, str | None]:
    choices = response.get("choices") or []
    if not choices:
        return "", None
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    finish = choices[0].get("finish_reason")
    return str(content), finish


def _parse_planner_reply(text: str) -> tuple[str, PlanImportSchema | None, str | None]:
    matches = list(_JSON_FENCE_RE.finditer(text))
    if not matches:
        return text.strip(), None, None
    last = matches[-1]
    raw_json = last.group(1).strip()
    reply = (text[: last.start()] + text[last.end() :]).strip()
    try:
        data = json.loads(raw_json)
        plan = PlanImportSchema.model_validate(data)
        return reply, plan, None
    except Exception as exc:
        return reply or text.strip(), None, str(exc)


class PlanModeService:
    @staticmethod
    async def converse(
        history: list[ChatTurnSchema],
        workspace_overview: dict[str, Any],
        *,
        client: OpenAICompatibleClient | None = None,
    ) -> PlanModeResponseSchema:
        cfg = AgentProviderFactory.get_config()
        logger.info(
            "Planner provider: %s model: %s",
            cfg["provider"],
            cfg["model"],
        )

        messages = build_planner_messages(
            [turn.model_dump() for turn in history],
            workspace_overview,
        )
        chat_client = client or _default_planner_client()
        timeout = float(cfg["timeout_seconds"])

        try:
            raw = await chat_client.chat_completion(
                messages=messages,
                model=cfg["model"],
                tools=None,
                timeout_seconds=timeout,
            )
        except LLMHttpError as exc:
            if exc.status_code in (408, 504):
                raise PlannerTimeoutError("Planner request timed out") from exc
            if exc.status_code in (401, 403):
                raise PlannerAuthError("Planner authentication failed") from exc
            raise PlannerProviderError(f"Planner HTTP {exc.status_code}") from exc
        except TimeoutError as exc:
            raise PlannerTimeoutError("Planner request timed out") from exc

        content, finish_reason = _extract_content(raw)
        reply, proposed_plan, parse_error = _parse_planner_reply(content)

        log_event(
            "plan_mode_converse",
            "completed",
            detail={
                "history_len": len(history),
                "has_plan": proposed_plan is not None,
                "model": cfg["model"],
                "parse_error": parse_error,
            },
        )
        return PlanModeResponseSchema(
            reply=reply,
            proposed_plan=proposed_plan,
            parse_error=parse_error,
            raw_finish_reason=finish_reason,
        )
