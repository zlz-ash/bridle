"""LLM-backed decision helper for main-agent loop."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from bridle.engine.openai_client import HttpOpenAICompatibleClient

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


@dataclass(frozen=True)
class Decision:
    action: str
    node_id: str = ""
    reply: str = ""
    reason: str = ""


class DeepSeekDecider:
    def __init__(self, api_key: str, model: str, *, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or os.environ.get("BRIDLE_AGENT_MODEL", "deepseek-chat")
        self._base_url = base_url or os.environ.get("BRIDLE_AGENT_BASE_URL", "https://api.deepseek.com")
        self._proxy = os.environ.get("HTTPS_PROXY", os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890"))

    def decide(
        self,
        chat_history: list[dict[str, Any]],
        plan: dict[str, Any],
        eligible: list[dict[str, Any]],
        *,
        failed_runs: list[dict[str, Any]] | None = None,
    ) -> Decision:
        failed = list(failed_runs or [])
        if not self._api_key:
            return self._fallback(chat_history, eligible)
        client = HttpOpenAICompatibleClient(
            api_key=self._api_key,
            base_url=self._base_url,
            proxy=self._proxy,
        )
        system = (
            "You are the Bridle main agent. You MUST respond with JSON ONLY (no prose, no fence).\n"
            'Schema: {"action": <one of: select_node|reply|wait|done>, "node_id": <string>, '
            '"reply": <string>, "reason": <string>}\n\n'
            "Rules:\n"
            "1. If `eligible` array is non-empty AND the user just asked to execute / continue / 开始 / 派发 / 跑节点, "
            "you MUST emit `select_node` with `node_id` set to ONE of the UUIDs in `eligible[*].node_id`. "
            "DO NOT make up node_ids. DO NOT use `plan_node_id`. DO NOT promise execution in `reply` while choosing action=reply.\n"
            "2. action=reply only when conversing (greetings, clarifications, summaries). reply MUST be non-empty.\n"
            "3. action=wait only when waiting for an in-flight node to finish; reason MUST explain what you're waiting for.\n"
            "4. action=done only when the whole task is finished.\n"
            "5. NEVER pretend you executed a node — UI shows actual node status from DB, not your reply text.\n"
            "6. When `failed_runs` is non-empty, your `reply` MUST acknowledge the most recent failure "
            "with its summary, and either:\n"
            "   - emit select_node again (retry) if attempts < 2 and the failure looks fixable\n"
            "   - emit reply with concrete diagnosis if the failure needs human attention\n"
            "   DO NOT silently ignore failures."
        )
        user_content = json.dumps(
            {
                "chat": chat_history[-20:],
                "plan": plan,
                "eligible": eligible,
                "failed_runs": failed[-3:],
            },
            ensure_ascii=False,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        try:
            response = asyncio.run(
                client.chat_completion(
                    messages=messages,
                    model=self._model,
                    timeout_seconds=60.0,
                )
            )
            choices = response.get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
            text = str(message.get("content") or "")
            return self._parse_decision(text, eligible)
        except Exception:
            return self._fallback(chat_history, eligible)

    def _parse_decision(self, text: str, eligible: list[dict[str, Any]]) -> Decision:
        payload_text = text.strip()
        match = _JSON_FENCE_RE.search(payload_text)
        if match:
            payload_text = match.group(1).strip()
        data = json.loads(payload_text)
        action = str(data.get("action", "wait"))
        node_id = str(data.get("node_id", ""))
        reply = str(data.get("reply", ""))
        reason = str(data.get("reason", ""))

        if action == "select_node":
            eligible_ids = {n["node_id"] for n in eligible if n.get("node_id")}
            if not node_id and eligible:
                node_id = str(eligible[0].get("node_id", ""))
            if not eligible_ids or node_id not in eligible_ids:
                return Decision(
                    action="reply",
                    reply=(
                        f"⚠️ 模型选择的节点 id `{node_id}` 不在可执行列表内。"
                        f"可执行节点：{sorted(eligible_ids) or '无'}。请重新指示。"
                    ),
                    reason="select_node_not_in_eligible",
                )

        return Decision(action=action, node_id=node_id, reply=reply, reason=reason)

    @staticmethod
    def _fallback(chat_history: list[dict[str, Any]], eligible: list[dict[str, Any]]) -> Decision:
        if eligible:
            node = eligible[0]
            node_id = str(node.get("node_id") or node.get("plan_node_id") or "")
            return Decision(action="select_node", node_id=node_id, reason="fallback_eligible")
        last_user = next((m for m in reversed(chat_history) if m.get("role") == "user"), None)
        if last_user:
            return Decision(action="reply", reply="收到，当前没有可执行节点。", reason="fallback_reply")
        return Decision(action="wait", reason="fallback_wait")
