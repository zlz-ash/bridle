"""DeepSeek agent provider with tool-call loop."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from bridle.engine.agent_tool_registry import AgentToolRegistry
from bridle.engine.context_template import ContextTemplateBuilder
from bridle.engine.deepseek_client import DeepSeekHttpError
from bridle.engine.deepseek_tools_schema import build_deepseek_tools
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.engine.proposal_test_validator import validate_proposal_tests_to_run
from bridle.engine.tool_budget import ToolBudgetLimits, ToolBudgetTracker, summarize_tool_args
from bridle.logging.jsonl import log_event
from bridle.schemas.proposal import AgentContext, AgentProposalSchema

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


class ToolCallTracker:
    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._non_retryable_max = int(os.environ.get("BRIDLE_CIRCUIT_NON_RETRYABLE_MAX", "1"))
        self._retryable_max = int(os.environ.get("BRIDLE_CIRCUIT_RETRYABLE_MAX", "2"))
        self._timeout_max = int(os.environ.get("BRIDLE_CIRCUIT_TIMEOUT_MAX", "2"))
        self._test_command_max = int(os.environ.get("BRIDLE_CIRCUIT_TEST_COMMAND_MAX", "5"))

    def _key(self, tool_name: str, arguments: dict[str, Any]) -> str:
        args_json = json.dumps(arguments, sort_keys=True, default=str)
        args_hash = hashlib.sha256(args_json.encode()).hexdigest()[:8]
        return f"{tool_name}:{args_hash}"

    def record_result(self, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        k = self._key(tool_name, arguments)
        if k not in self._states:
            self._states[k] = {
                "attempts": 0,
                "consecutive_failures": 0,
                "last_error_code": None,
                "last_retryable": None,
            }
        state = self._states[k]
        state["attempts"] += 1
        if result.get("status") == "failed":
            state["consecutive_failures"] += 1
            state["last_error_code"] = result.get("error_code")
            state["last_retryable"] = result.get("retryable", False)
        else:
            state["consecutive_failures"] = 0
            state["last_error_code"] = None
            state["last_retryable"] = None

    def should_circuit_open(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        k = self._key(tool_name, arguments)
        state = self._states.get(k)
        if state is None:
            return None
        if state["consecutive_failures"] == 0:
            return None
        last_retryable = state.get("last_retryable", False)
        last_error_code = state.get("last_error_code", "")
        timeout_errors = {"TestCommandTimeout", "WebSearchTimeout"}
        if tool_name == "run_allowed_tests" and last_retryable:
            max_failures = self._test_command_max
        elif last_error_code in timeout_errors:
            max_failures = self._timeout_max
        elif last_retryable:
            max_failures = self._retryable_max
        else:
            max_failures = self._non_retryable_max
        if state["consecutive_failures"] >= max_failures:
            return {
                "status": "failed",
                "error_code": "tool_circuit_open",
                "message": (
                    f"Tool '{tool_name}' circuit breaker opened after "
                    f"{state['consecutive_failures']} consecutive failures. "
                    f"Change arguments, try a different tool, or report_blocked."
                ),
                "category": "circuit_breaker",
                "retryable": False,
                "attempts": state["attempts"],
                "consecutive_failures": state["consecutive_failures"],
                "next_action": "change_arguments_or_report_blocked",
            }
        return None

    def get_state(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        k = self._key(tool_name, arguments)
        return self._states.get(k)

    def enrich_result(self, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        state = self.get_state(tool_name, arguments)
        if state is None:
            return result
        result["attempts"] = state["attempts"]
        result["consecutive_failures"] = state["consecutive_failures"]
        result["last_error_code"] = state["last_error_code"]
        result["last_retryable"] = state["last_retryable"]
        return result


class DeepSeekProviderError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str = "",
        *,
        model_final_response_preview: str = "",
        response_debug: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.model_final_response_preview = model_final_response_preview
        self.response_debug = dict(response_debug or {})
        super().__init__(message or error_code)


_MODEL_RESPONSE_PREVIEW_MAX = 500
_BEARER_PREVIEW_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+")
_BASIC_PREVIEW_RE = re.compile(r"(?i)Basic\s+[A-Za-z0-9+/=._\-]+")
_SK_PREVIEW_RE = re.compile(r"sk-[A-Za-z0-9]{8,}")
_JSON_KV_RE = re.compile(
    r'("(?:\\.|[^"\\])+")\s*:\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,}}\]]+)',
)
_ENV_KV_RE = re.compile(
    r"([A-Za-z_][\w-]*)\s*([:=])\s*"
    r'("(?:\\.|[^"\\])*"|\'[^\']*\'|[^\s#,;\]}}]+)',
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(Authorization)\s*:\s*(Bearer\s+\S+|Basic\s+\S+|\S+)",
)


def _key_name_is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if re.search(r"api[_-]?key|apikey", normalized):
        return True
    if any(part in normalized for part in ("authorization", "password", "secret")):
        return True
    return "token" in normalized


def _strip_wrapping_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _redact_authorization_value(value: str) -> str:
    stripped = _strip_wrapping_quotes(value)
    lower = stripped.lower()
    if lower.startswith("bearer "):
        return "Bearer ***"
    if lower.startswith("basic "):
        return "Basic ***"
    return "***"


def sanitize_model_response_text(content: str) -> str:
    text = str(content)

    def _redact_auth_header(match: re.Match[str]) -> str:
        return f"{match.group(1)}: {_redact_authorization_value(match.group(2))}"

    text = _AUTH_HEADER_RE.sub(_redact_auth_header, text)
    text = _BEARER_PREVIEW_RE.sub("Bearer ***", text)
    text = _BASIC_PREVIEW_RE.sub("Basic ***", text)
    text = _SK_PREVIEW_RE.sub("sk-***", text)

    def _redact_json_kv(match: re.Match[str]) -> str:
        key_quoted = match.group(1)
        key = key_quoted[1:-1]
        if not _key_name_is_sensitive(key):
            return match.group(0)
        if key.lower().replace("-", "_") == "authorization":
            return f'{key_quoted}: "{_redact_authorization_value(match.group(2))}"'
        return f"{key_quoted}: ***"

    def _redact_env_kv(match: re.Match[str]) -> str:
        key, sep, value = match.group(1), match.group(2), match.group(3)
        if key.lower().replace("-", "_") == "authorization":
            return match.group(0)
        if not _key_name_is_sensitive(key):
            return match.group(0)
        return f"{key}{sep}***"

    text = _JSON_KV_RE.sub(_redact_json_kv, text)
    text = _ENV_KV_RE.sub(_redact_env_kv, text)
    return text


def _sorted_dict_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value.keys())
    return []


def _summarize_top_level_when_no_choices(
    response: dict[str, Any],
    optional_preview: Any,
) -> dict[str, Any]:
    error = response.get("error") if isinstance(response.get("error"), dict) else None
    data = response.get("data")
    result = response.get("result")
    output = response.get("output")
    summary = {
        "top_level_keys": _sorted_dict_keys(response),
        "response_type": type(response).__name__,
        "has_error": error is not None,
        "error_keys": _sorted_dict_keys(error),
        "error_code": "",
        "error_message_preview": "",
        "has_data": data is not None,
        "data_keys": _sorted_dict_keys(data),
        "has_result": result is not None,
        "result_keys": _sorted_dict_keys(result),
        "has_output": output is not None,
        "output_type": type(output).__name__ if output is not None else "",
    }
    if error is not None:
        summary["error_code"] = str(error.get("code") or error.get("type") or "")
        message = error.get("message") or error.get("msg") or ""
        summary["error_message_preview"] = optional_preview(message)
    return summary


def summarize_chat_response_envelope(
    response: dict[str, Any],
    *,
    choice_index: int = 0,
) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list):
        choices = []
    choice = choices[choice_index] if choice_index < len(choices) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    reasoning = message.get("reasoning_content")
    refusal = message.get("refusal")
    annotations = message.get("annotations")
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []

    def _optional_preview(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        return preview_model_response(text, max_len=120)

    summary = {
        "choice_count": len(choices),
        "finish_reason": str(choice.get("finish_reason") or ""),
        "message_keys": sorted(str(key) for key in message.keys()),
        "content_is_null": content is None,
        "content_length": 0 if content is None else len(str(content)),
        "tool_call_count": len(tool_calls),
        "has_reasoning_content": bool(_optional_preview(reasoning)),
        "has_refusal": bool(_optional_preview(refusal)),
        "has_annotations": annotations is not None and bool(annotations),
        "usage_keys": sorted(str(key) for key in usage.keys()),
        "reasoning_content_preview": _optional_preview(reasoning),
        "refusal_preview": _optional_preview(refusal),
    }
    if len(choices) == 0:
        summary.update(_summarize_top_level_when_no_choices(response, _optional_preview))
    return summary


def _assistant_tool_round_message(message: dict[str, Any]) -> dict[str, Any]:
    tool_calls = message.get("tool_calls") or []
    payload: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content"),
        "tool_calls": tool_calls,
    }
    reasoning = message.get("reasoning_content")
    if reasoning is not None and str(reasoning).strip():
        payload["reasoning_content"] = reasoning
    return payload


def preview_model_response(content: str, max_len: int = _MODEL_RESPONSE_PREVIEW_MAX) -> str:
    text = sanitize_model_response_text(content)
    if len(text) <= max_len:
        return text
    half = max_len // 2
    return text[:half] + "...[truncated]..." + text[-half:]


class DeepSeekAgentProvider:
    name = "deepseek"

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        max_tool_rounds: int = 8,
        max_tool_calls: int = 32,
        max_wall_seconds: float = 300.0,
        registry: AgentToolRegistry,
        strict_tools: bool = False,
        timeout_seconds: float = 120,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tool_rounds = max(1, int(max_tool_rounds))
        self._budget_limits = ToolBudgetLimits(
            max_rounds=self._max_tool_rounds,
            max_tool_calls=max(1, int(max_tool_calls)),
            max_wall_seconds=max(1.0, float(max_wall_seconds)),
        )
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
        tracker = ToolCallTracker()
        budget_tracker = ToolBudgetTracker(self._budget_limits, start_time=start)

        try:
            while True:
                exhausted = budget_tracker.check_before_round()
                if exhausted:
                    report = budget_tracker.build_exhausted_report(exhausted)
                    raise DeepSeekProviderError(
                        "tool_budget_exhausted",
                        f"Tool budget exhausted: {exhausted}",
                        response_debug=report,
                    )
                budget_tracker.begin_round()

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
                    messages.append(_assistant_tool_round_message(message))
                    for tc in tool_calls:
                        exhausted = budget_tracker.check_before_tool_call()
                        if exhausted:
                            report = budget_tracker.build_exhausted_report(exhausted)
                            raise DeepSeekProviderError(
                                "tool_budget_exhausted",
                                f"Tool budget exhausted: {exhausted}",
                                response_debug=report,
                            )
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
                        circuit_result = tracker.should_circuit_open(tool_name, args)
                        if circuit_result is not None:
                            result = circuit_result
                            log_event(
                                "deepseek_tool_circuit_open",
                                "failed",
                                run_id=policy.run_id,
                                node_id=policy.node_id,
                                detail={
                                    "tool_name": tool_name,
                                    "attempts": circuit_result.get("attempts"),
                                    "consecutive_failures": circuit_result.get("consecutive_failures"),
                                },
                            )
                        else:
                            result = await registry.execute(
                                tool_name,
                                args,
                                tool_call_id=str(tc.get("id", "")),
                            )
                            tracker.record_result(tool_name, args, result)
                            result = tracker.enrich_result(tool_name, args, result)
                            budget_tracker.note_tool_result(tool_name, result)
                        budget_tracker.record_tool_call(
                            tool_name=tool_name,
                            args_summary=summarize_tool_args(args),
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
                    content_str = str(content)
                    proposal = parse_proposal_content(content_str)
                    _validate_proposal(proposal, context, model_content=content_str)
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
                    response_debug=summarize_chat_response_envelope(response),
                )

            report = budget_tracker.build_exhausted_report("rounds")
            raise DeepSeekProviderError(
                "tool_budget_exhausted",
                "Tool budget exhausted: rounds",
                response_debug=report,
            )
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
        raise DeepSeekProviderError(
            "invalid_agent_proposal",
            str(exc),
            model_final_response_preview=preview_model_response(content),
        ) from exc
    try:
        return AgentProposalSchema.model_validate(data)
    except Exception as exc:
        raise DeepSeekProviderError(
            "invalid_agent_proposal",
            str(exc),
            model_final_response_preview=preview_model_response(content),
        ) from exc


def _validate_proposal(
    proposal: AgentProposalSchema,
    context: AgentContext,
    *,
    model_content: str = "",
) -> None:
    preview = preview_model_response(model_content) if model_content else ""
    file_patches = [fp.model_dump() for fp in proposal.file_patches]
    path_errors = ProposalPathValidator.validate(file_patches, context.allowed_files)
    if path_errors:
        raise DeepSeekProviderError(
            "PathBoundaryError",
            "; ".join(path_errors),
            model_final_response_preview=preview,
        )

    snap = context.tool_capabilities.get("sandbox", {}) if context.tool_capabilities else {}
    cmd_errors = validate_proposal_tests_to_run(proposal, snap, context.tests)
    if cmd_errors:
        raise DeepSeekProviderError(
            "CommandPolicyError",
            "; ".join(cmd_errors),
            model_final_response_preview=preview,
        )


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
