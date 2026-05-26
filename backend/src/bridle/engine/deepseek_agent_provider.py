"""DeepSeek agent provider with tool-call loop."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from bridle.engine.agent_tool_registry import AgentToolRegistry
from bridle.engine.context_template import ContextTemplateBuilder
from bridle.engine.deepseek_client import DeepSeekHttpError
from bridle.engine.deepseek_tools_schema import build_deepseek_tools
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.engine.proposal_test_validator import validate_proposal_tests_to_run
from bridle.logging.jsonl import log_event
from bridle.schemas.proposal import AgentContext, AgentProposalSchema

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


class DeepSeekProviderError(Exception):
    def __init__(self, error_code: str, message: str = "") -> None:
        self.error_code = error_code
        super().__init__(message or error_code)


class DeepSeekAgentProvider:
    name = "deepseek"

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_tool_rounds: int = 8,
        registry: AgentToolRegistry,
        strict_tools: bool = False,
        timeout_seconds: float = 120,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tool_rounds = max(1, int(max_tool_rounds))
        self._registry = registry
        self._strict_tools = strict_tools
        self._timeout_seconds = timeout_seconds
        self._run_id = run_id
        self._node_id = node_id

    async def generate(self, context: AgentContext) -> AgentProposalSchema:
        registry = self._registry
        policy = registry._policy  # noqa: SLF001 — worker-built registry shares policy
        tool_descriptors = registry.tool_descriptors()
        tool_context = [d.model_dump() for d in tool_descriptors]
        child_agent_results = (
            context.tool_capabilities.get("child_agent_results", [])
            if context.tool_capabilities
            else []
        )
        builder = ContextTemplateBuilder(
            context,
            tool_context=tool_context,
            child_agent_results=child_agent_results,
            run_id=self._run_id,
            node_id=self._node_id,
        )
        messages = builder.build_messages()
        tools = build_deepseek_tools(strict=self._strict_tools)

        log_event(
            "deepseek_request_started",
            "started",
            run_id=policy.run_id,
            node_id=policy.node_id,
            detail={"provider": self.name, "model": self._model},
        )
        start = time.monotonic()

        try:
            for _round_idx in range(self._max_tool_rounds):
                response = await self._client.chat_completion(
                    messages=messages,
                    model=self._model,
                    tools=tools,
                    timeout_seconds=self._timeout_seconds,
                )
                choice = (response.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                finish_reason = choice.get("finish_reason")
                tool_calls = message.get("tool_calls") or []

                if tool_calls:
                    messages.append({
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    })
                    for tc in tool_calls:
                        fn = tc.get("function") or {}
                        tool_name = fn.get("name", "")
                        raw_args = fn.get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        except json.JSONDecodeError as exc:
                            raise DeepSeekProviderError(
                                "invalid_tool_arguments",
                                f"Cannot parse tool arguments: {exc}",
                            ) from exc
                        result = await registry.execute(
                            tool_name,
                            args,
                            tool_call_id=str(tc.get("id", "")),
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": tool_name,
                            "content": registry.tool_result_content(result),
                        })
                    continue

                content = message.get("content")
                if content and str(content).strip():
                    proposal = parse_proposal_content(str(content))
                    _validate_proposal(proposal, context)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    log_event(
                        "deepseek_final_proposal_parsed",
                        "completed",
                        run_id=policy.run_id,
                        node_id=policy.node_id,
                        duration_ms=duration_ms,
                        detail={
                            "provider": self.name,
                            "model": self._model,
                            "finish_reason": finish_reason,
                            "token_usage": response.get("usage"),
                        },
                    )
                    log_event(
                        "deepseek_request_completed",
                        "completed",
                        run_id=policy.run_id,
                        node_id=policy.node_id,
                        duration_ms=duration_ms,
                        detail={"provider": self.name, "model": self._model},
                    )
                    return proposal

                raise DeepSeekProviderError(
                    "invalid_agent_proposal",
                    "Assistant returned empty content without tool calls",
                )

            raise DeepSeekProviderError("tool_round_limit_exceeded")
        except DeepSeekProviderError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log_event(
                "deepseek_final_proposal_invalid",
                "failed",
                run_id=policy.run_id,
                node_id=policy.node_id,
                duration_ms=duration_ms,
                detail={
                    "provider": self.name,
                    "model": self._model,
                    "error_code": exc.error_code,
                },
            )
            raise
        except DeepSeekHttpError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            code = _map_http_error(exc.status_code)
            log_event(
                "deepseek_request_failed",
                "failed",
                run_id=policy.run_id,
                node_id=policy.node_id,
                duration_ms=duration_ms,
                detail={
                    "provider": self.name,
                    "model": self._model,
                    "error_code": code,
                    "http_status": exc.status_code,
                },
            )
            raise DeepSeekProviderError(code, exc.body[:500]) from exc
        except TimeoutError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log_event(
                "deepseek_request_failed",
                "failed",
                run_id=policy.run_id,
                node_id=policy.node_id,
                duration_ms=duration_ms,
                detail={"error_code": "deepseek_timeout", "provider": self.name, "model": self._model},
            )
            raise DeepSeekProviderError("deepseek_timeout") from exc
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log_event(
                "deepseek_request_failed",
                "failed",
                run_id=policy.run_id,
                node_id=policy.node_id,
                duration_ms=duration_ms,
                detail={"error_code": type(exc).__name__, "provider": self.name, "model": self._model},
            )
            raise DeepSeekProviderError(type(exc).__name__, str(exc)) from exc


def parse_proposal_content(content: str) -> AgentProposalSchema:
    text = content.strip()
    match = _JSON_FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeepSeekProviderError("invalid_agent_proposal", str(exc)) from exc
    try:
        return AgentProposalSchema.model_validate(data)
    except Exception as exc:
        raise DeepSeekProviderError("invalid_agent_proposal", str(exc)) from exc


def _validate_proposal(proposal: AgentProposalSchema, context: AgentContext) -> None:
    file_patches = [fp.model_dump() for fp in proposal.file_patches]
    path_errors = ProposalPathValidator.validate(file_patches, context.allowed_files)
    if path_errors:
        raise DeepSeekProviderError("PathBoundaryError", "; ".join(path_errors))

    snap = context.tool_capabilities.get("sandbox", {}) if context.tool_capabilities else {}
    cmd_errors = validate_proposal_tests_to_run(proposal, snap, context.tests)
    if cmd_errors:
        raise DeepSeekProviderError("CommandPolicyError", "; ".join(cmd_errors))


def _map_http_error(status_code: int) -> str:
    if status_code in (401, 403):
        return "deepseek_auth_error"
    if status_code == 429:
        return "deepseek_rate_limited"
    if status_code >= 500:
        return "deepseek_server_error"
    if status_code == 408:
        return "deepseek_timeout"
    return "deepseek_request_error"
