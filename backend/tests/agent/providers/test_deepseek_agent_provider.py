"""Tests for DeepSeekAgentProvider with mock client."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

import bridle.agent.providers.deepseek_agent_provider as deepseek_module
from bridle.agent.providers.deepseek_agent_provider import (
    DeepSeekAgentProvider,
    DeepSeekProviderError,
    parse_proposal_content,
    preview_model_response,
    sanitize_model_response_text,
    summarize_chat_response_envelope,
)
from bridle.agent.runtime.schemas import AgentContext, AgentProposalSchema
from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.registry import AgentToolRegistry
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor


def _ctx(test_workspace: Path, **overrides) -> AgentContext:
    snap = {
        "run_id": "run-ds",
        "node_id": "node-ds",
        "workspace_root": str(test_workspace),
        "allowed_files": ["src/a.py"],
        "allowed_test_commands": ["echo ok"],
    }
    base = {
        "instruction": "Implement feature",
        "node": {"id": "n1", "goal": "g"},
        "allowed_files": ["src/a.py"],
        "tests": ["echo ok"],
        "metrics": {},
        "constraints": {},
        "review_checks": [],
        "expected_outputs": {},
        "accessible_context": {},
        "tool_capabilities": {"sandbox": snap},
    }
    base.update(overrides)
    return AgentContext(**base)


def _registry(test_workspace: Path) -> AgentToolRegistry:
    policy = SandboxPolicy.for_run(
        run_id="run-ds",
        node_id="node-ds",
        workspace_root=test_workspace,
        allowed_files=["src/a.py"],
        node_tests=["echo ok"],
    )
    (test_workspace / "src").mkdir(parents=True, exist_ok=True)
    (test_workspace / "src/a.py").write_text("x=1", encoding="utf-8")
    return AgentToolRegistry(SandboxedToolExecutor(policy))


def _completed_response(
    *,
    status: str = "completed",
    summary: str = "done",
    reason: str = "",
) -> dict:
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": json.dumps({
                    "terminal_status": status,
                    "reason": reason,
                    "summary": summary,
                    "file_patches": [],
                    "tests_to_run": [],
                }),
            },
            "finish_reason": "stop",
        }],
        "usage": {},
    }


def _tool_call_response(index: int) -> dict:
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {
                        "name": "report_blocked",
                        "arguments": json.dumps({"reason": f"round-{index}"}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    }


class _ObservationRecorder:
    def __init__(self) -> None:
        self.generations: list[dict] = []

    def record_generation(self, **kwargs) -> None:
        self.generations.append({
            "name": kwargs["name"],
            "input_summary": copy.deepcopy(kwargs["input_summary"]),
            "output_summary": copy.deepcopy(kwargs["output_summary"]),
            "metadata": copy.deepcopy(kwargs.get("metadata") or {}),
        })


class TestParseProposalContent:
    def test_parses_raw_json(self) -> None:
        raw = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "done",
            "file_patches": [],
            "tests_to_run": [],
        })
        p = parse_proposal_content(raw)
        assert p.summary == "done"

    def test_parses_fenced_json(self) -> None:
        raw = (
            '```json\n{"terminal_status":"completed","reason":"",'
            '"summary":"s","file_patches":[],"tests_to_run":[]}\n```'
        )
        p = parse_proposal_content(raw)
        assert p.summary == "s"


class TestDeepSeekAgentProvider:
    @pytest.mark.asyncio
    async def test_single_round_final_json(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "DeepSeek proposal",
            "file_patches": [{"path": "src/a.py", "change_type": "modify", "diff": "d"}],
            "tests_to_run": ["echo ok"],
        })

        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": proposal_json},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                }

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        result = await provider.generate(_ctx(test_workspace))
        assert result.summary == "DeepSeek proposal"

    @pytest.mark.asyncio
    async def test_multi_round_tool_call_then_proposal(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "after tools",
            "file_patches": [],
            "tests_to_run": [],
        })
        calls = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "report_blocked",
                                "arguments": json.dumps({"reason": "test-only completed call"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {},
            },
            {
                "choices": [{
                    "message": {"role": "assistant", "content": proposal_json},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ]

        class Client:
            def __init__(self) -> None:
                self.i = 0

            async def chat_completion(self, **kwargs):
                resp = calls[self.i]
                self.i += 1
                return resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        client = Client()
        provider = DeepSeekAgentProvider(
            client=client,
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        result = await provider.generate(_ctx(test_workspace))
        assert result.summary == "after tools"
        assert client.i == 2

    @pytest.mark.asyncio
    async def test_max_wall_seconds_exceeded(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c",
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": json.dumps({"path": "src/a.py"}),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }
        clock = [100.0, 100.0, 102.0, 200.0]

        def fake_monotonic() -> float:
            if len(clock) > 1:
                return clock.pop(0)
            return clock[0]

        monkeypatch.setattr("bridle.agent.tools.budget.time.monotonic", fake_monotonic)
        monkeypatch.setattr("bridle.agent.providers.deepseek_agent_provider.time.monotonic", fake_monotonic)

        class Client:
            async def chat_completion(self, **kwargs):
                return tool_resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_wall_seconds=1.0,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "wall_seconds"

    @pytest.mark.asyncio
    async def test_out_of_boundary_patch_fails(self, test_workspace: Path) -> None:
        bad = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "bad",
            "file_patches": [{"path": "src/other.py", "change_type": "modify", "diff": "d"}],
            "tests_to_run": [],
        })

        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": bad},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(), model="m", registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "PathBoundaryError"

    @pytest.mark.asyncio
    async def test_empty_allowlist_rejects_context_tests_command(self, test_workspace: Path) -> None:
        raw = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "bad allowlist",
            "file_patches": [],
            "tests_to_run": ["echo ok"],
        })

        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": raw},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        snap = {
            "run_id": "run-ds",
            "node_id": "node-ds",
            "workspace_root": str(test_workspace),
            "allowed_files": ["src/a.py"],
            "allowed_test_commands": [],
        }
        ctx = AgentContext(
            instruction="x",
            node={"id": "n1"},
            allowed_files=["src/a.py"],
            tests=["echo ok"],
            metrics={},
            constraints={},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
            tool_capabilities={"sandbox": snap},
        )
        provider = DeepSeekAgentProvider(
            client=Client(), model="m", registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(ctx)
        assert exc.value.error_code == "CommandPolicyError"


class TestToolCircuitBreaker:
    @pytest.mark.asyncio
    async def test_failed_tool_message_includes_attempt_counters(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
            "terminal_status": "blocked",
            "reason": "tool execution failed",
            "summary": "blocked after failure",
            "file_patches": [],
            "tests_to_run": [],
        })
        calls = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps({"path": "../secret.py"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {},
            },
            {
                "choices": [{
                    "message": {"role": "assistant", "content": proposal_json},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ]

        class Client:
            def __init__(self) -> None:
                self.i = 0
                self.messages_seen: list[dict] = []

            async def chat_completion(self, **kwargs):
                self.messages_seen = list(kwargs["messages"])
                resp = calls[self.i]
                self.i += 1
                return resp

        client = Client()
        provider = DeepSeekAgentProvider(
            client=client,
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        await provider.generate(_ctx(test_workspace))
        tool_messages = [m for m in client.messages_seen if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        payload = json.loads(tool_messages[0]["content"])
        assert payload["status"] == "failed"
        assert payload["attempts"] == 1
        assert payload["consecutive_failures"] == 1

    @pytest.mark.asyncio
    async def test_same_args_failure_attempt_counters_increment(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
            "terminal_status": "blocked",
            "reason": "tool execution repeatedly failed",
            "summary": "blocked after repeated failure",
            "file_patches": [],
            "tests_to_run": [],
        })
        repeated_call = {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_repeat",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": "docs"}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }
        calls = [
            {"choices": [repeated_call], "usage": {}},
            {"choices": [repeated_call], "usage": {}},
            {
                "choices": [{
                    "message": {"role": "assistant", "content": proposal_json},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ]

        class Client:
            def __init__(self) -> None:
                self.i = 0
                self.messages_seen: list[dict] = []

            async def chat_completion(self, **kwargs):
                self.messages_seen = list(kwargs["messages"])
                resp = calls[self.i]
                self.i += 1
                return resp

        class RetryableRegistry(AgentToolRegistry):
            async def execute(self, tool_name: str, arguments: dict, *, tool_call_id: str) -> dict:
                return {
                    "status": "failed",
                    "error_code": "WebSearchError",
                    "category": "external",
                    "retryable": True,
                }

        client = Client()
        provider = DeepSeekAgentProvider(
            client=client,
            model="deepseek-chat",
            registry=RetryableRegistry(_registry(test_workspace)._executor),
        )
        await provider.generate(_ctx(test_workspace))
        tool_messages = [json.loads(m["content"]) for m in client.messages_seen if m.get("role") == "tool"]
        assert tool_messages[0]["error_code"] == "WebSearchError"
        assert tool_messages[0]["category"] == "external"
        assert tool_messages[0]["retryable"] is True
        assert "attempts" not in tool_messages[0]
        assert tool_messages[1]["attempts"] == 2
        assert tool_messages[1]["consecutive_failures"] == 2

    @pytest.mark.asyncio
    async def test_success_tool_message_includes_attempt_and_resets_failures(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "after success",
            "file_patches": [],
            "tests_to_run": [],
        })
        calls = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_success",
                            "type": "function",
                            "function": {
                                "name": "report_blocked",
                                "arguments": json.dumps({"reason": "test-only completed call"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {},
            },
            {
                "choices": [{
                    "message": {"role": "assistant", "content": proposal_json},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ]

        class Client:
            def __init__(self) -> None:
                self.i = 0
                self.messages_seen: list[dict] = []

            async def chat_completion(self, **kwargs):
                self.messages_seen = list(kwargs["messages"])
                resp = calls[self.i]
                self.i += 1
                return resp

        client = Client()
        provider = DeepSeekAgentProvider(
            client=client,
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        await provider.generate(_ctx(test_workspace))
        tool_messages = [m for m in client.messages_seen if m.get("role") == "tool"]
        payload = json.loads(tool_messages[0]["content"])
        assert payload["status"] == "completed"
        assert payload["attempts"] == 1
        assert payload["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_same_args_non_retryable_circuits_after_one_failure(self, test_workspace: Path) -> None:
        call_count = 0

        class Client:
            async def chat_completion(self, **kwargs):
                nonlocal call_count
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": f"call_{call_count}",
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "arguments": json.dumps({"path": "../secret.py"}),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "rounds"

    @pytest.mark.asyncio
    async def test_circuit_breaker_returns_circuit_open(self, test_workspace: Path) -> None:
        from bridle.agent.providers.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args = {"path": "../secret.py"}
        result1 = {"status": "failed", "error_code": "PathBoundaryError", "category": "policy", "retryable": False}
        tracker.record_result("run_command", args, result1)
        circuit = tracker.should_circuit_open("run_command", args)
        assert circuit is not None
        assert circuit["error_code"] == "tool_circuit_open"
        assert circuit["consecutive_failures"] == 1
        assert circuit["retryable"] is False

    @pytest.mark.asyncio
    async def test_different_args_no_circuit(self, test_workspace: Path) -> None:
        from bridle.agent.providers.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args1 = {"path": "../secret.py"}
        args2 = {"path": "src/a.py"}
        result1 = {"status": "failed", "error_code": "PathBoundaryError", "category": "policy", "retryable": False}
        tracker.record_result("run_command", args1, result1)
        circuit = tracker.should_circuit_open("run_command", args2)
        assert circuit is None

    @pytest.mark.asyncio
    async def test_retryable_allows_two_attempts(self, test_workspace: Path) -> None:
        from bridle.agent.providers.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args = {"commands": ["echo test"]}
        result1 = {"status": "failed", "error_code": "WebSearchError", "category": "external", "retryable": True}
        tracker.record_result("web_search", args, result1)
        circuit = tracker.should_circuit_open("web_search", args)
        assert circuit is None
        tracker.record_result("web_search", args, result1)
        circuit = tracker.should_circuit_open("web_search", args)
        assert circuit is not None
        assert circuit["error_code"] == "tool_circuit_open"

    @pytest.mark.asyncio
    async def test_run_command_retryable_allows_second_attempt(self, test_workspace: Path) -> None:
        from bridle.agent.providers.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args = {"commands": ["python -m pytest tests/ -q"]}
        fail = {
            "status": "failed",
            "error_code": "TestCommandFailed",
            "category": "test_failure",
            "retryable": True,
        }
        tracker.record_result("run_command", args, fail)
        assert tracker.should_circuit_open("run_command", args) is None

    @pytest.mark.asyncio
    async def test_run_command_circuit_opens_at_test_command_max(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bridle.agent.providers.deepseek_agent_provider import ToolCallTracker

        monkeypatch.setenv("BRIDLE_CIRCUIT_TEST_COMMAND_MAX", "2")
        tracker = ToolCallTracker()
        args = {"commands": ["python -m pytest tests/ -q"]}
        fail = {
            "status": "failed",
            "error_code": "TestCommandFailed",
            "category": "test_failure",
            "retryable": True,
        }
        tracker.record_result("run_command", args, fail)
        assert tracker.should_circuit_open("run_command", args) is None
        tracker.record_result("run_command", args, fail)
        circuit = tracker.should_circuit_open("run_command", args)
        assert circuit is not None
        assert circuit["error_code"] == "tool_circuit_open"

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_failures(self, test_workspace: Path) -> None:
        from bridle.agent.providers.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args = {"path": "../secret.py"}
        fail = {"status": "failed", "error_code": "PathBoundaryError", "category": "policy", "retryable": False}
        tracker.record_result("run_command", args, fail)
        success = {"status": "completed", "category": "success", "retryable": False}
        tracker.record_result("run_command", args, success)
        circuit = tracker.should_circuit_open("run_command", args)
        assert circuit is None

    @pytest.mark.asyncio
    async def test_failure_log_has_error_code_and_duration(
        self,
        test_workspace: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="bridle")

        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": "not-json"},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(), model="m", registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError):
            await provider.generate(_ctx(test_workspace))

        invalid_logs = [
            r for r in caplog.records
            if getattr(r, "action", None) == "deepseek_final_proposal_invalid"
        ]
        assert len(invalid_logs) == 1
        record = invalid_logs[0]
        assert getattr(record, "detail", {}).get("error_code") == "invalid_agent_proposal"
        assert getattr(record, "duration_ms", None) is not None
        assert "sk-" not in caplog.text

    @pytest.mark.asyncio
    async def test_non_allowlist_tests_fails(self, test_workspace: Path) -> None:
        raw = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "bad tests",
            "file_patches": [],
            "tests_to_run": ["echo not-allowed"],
        })

        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": raw},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(), model="m", registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "CommandPolicyError"


class TestReasoningContentInToolLoop:
    @pytest.mark.asyncio
    async def test_tool_round_preserves_reasoning_content_in_next_request(
        self, test_workspace: Path
    ) -> None:
        proposal_json = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "after thinking tools",
            "file_patches": [],
            "tests_to_run": [],
        })
        calls = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "thinking-token",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps({"path": "src/a.py"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {},
            },
            {
                "choices": [{
                    "message": {"role": "assistant", "content": proposal_json},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ]
        captured: list[list[dict]] = []

        class Client:
            def __init__(self) -> None:
                self.i = 0

            async def chat_completion(self, **kwargs):
                captured.append(list(kwargs.get("messages") or []))
                resp = calls[self.i]
                self.i += 1
                return resp

        client = Client()
        provider = DeepSeekAgentProvider(
            client=client,
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        result = await provider.generate(_ctx(test_workspace))
        assert result.summary == "after thinking tools"
        assert len(captured) == 2
        second_request = captured[1]
        assistant_tool_msgs = [
            msg for msg in second_request
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        ]
        assert len(assistant_tool_msgs) == 1
        assert assistant_tool_msgs[0].get("reasoning_content") == "thinking-token"

    @pytest.mark.asyncio
    async def test_tool_round_without_reasoning_content_unchanged(
        self, test_workspace: Path
    ) -> None:
        proposal_json = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "plain tools",
            "file_patches": [],
            "tests_to_run": [],
        })
        calls = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps({"path": "src/a.py"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {},
            },
            {
                "choices": [{
                    "message": {"role": "assistant", "content": proposal_json},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ]
        captured: list[list[dict]] = []

        class Client:
            def __init__(self) -> None:
                self.i = 0

            async def chat_completion(self, **kwargs):
                captured.append(list(kwargs.get("messages") or []))
                resp = calls[self.i]
                self.i += 1
                return resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        await provider.generate(_ctx(test_workspace))
        assistant_tool_msgs = [
            msg for msg in captured[1]
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        ]
        assert len(assistant_tool_msgs) == 1
        assert "reasoning_content" not in assistant_tool_msgs[0]


class TestEmptyResponseDebug:
    def test_summarize_envelope_for_null_content(self) -> None:
        response = {
            "choices": [{
                "message": {"role": "assistant", "content": None, "reasoning_content": "hidden"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 0},
        }
        debug = summarize_chat_response_envelope(response)
        assert debug["choice_count"] == 1
        assert debug["finish_reason"] == "stop"
        assert debug["content_is_null"] is True
        assert debug["content_length"] == 0
        assert debug["tool_call_count"] == 0
        assert debug["has_reasoning_content"] is True
        assert "reasoning_content" in debug["message_keys"] or debug["reasoning_content_preview"]

    @pytest.mark.asyncio
    async def test_empty_content_receives_repair_feedback(self, test_workspace: Path) -> None:
        captured: list[list[dict]] = []
        responses = [
            {
                "choices": [{
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "stop",
                }],
                "usage": {"total_tokens": 10},
            },
            _completed_response(summary="repaired empty response"),
        ]

        class Client:
            async def chat_completion(self, **kwargs):
                captured.append(copy.deepcopy(kwargs["messages"]))
                return responses[len(captured) - 1]

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="m",
            registry=_registry(test_workspace),
        )
        proposal = await provider.generate(_ctx(test_workspace))
        assert proposal.terminal_status == "completed"
        assert proposal.summary == "repaired empty response"
        assert len(captured) == 2
        assert captured[1][-1]["role"] == "user"
        assert "terminal_status" in captured[1][-1]["content"]


class TestNoChoicesTopLevelDebug:
    def test_error_envelope_summarized_and_redacted(self) -> None:
        response = {
            "error": {
                "code": "bad_model",
                "message": "model not found API_KEY: xyz98765",
            },
        }
        debug = summarize_chat_response_envelope(response)
        assert debug["choice_count"] == 0
        assert "error" in debug["top_level_keys"]
        assert debug["has_error"] is True
        assert debug["error_code"] == "bad_model"
        assert "model not found" in debug["error_message_preview"]
        assert "xyz98765" not in debug["error_message_preview"]

    def test_data_wrapper_envelope(self) -> None:
        response = {
            "data": {
                "choices": [],
                "request_id": "req-1",
            },
        }
        debug = summarize_chat_response_envelope(response)
        assert debug["choice_count"] == 0
        assert debug["has_data"] is True
        assert "choices" in debug["data_keys"]

    def test_result_wrapper_envelope(self) -> None:
        response = {
            "result": {
                "output": {"text": "hello"},
                "status": "done",
            },
        }
        debug = summarize_chat_response_envelope(response)
        assert debug["choice_count"] == 0
        assert debug["has_result"] is True
        assert "output" in debug["result_keys"]

    def test_top_level_output_type(self) -> None:
        response = {"output": "plain-text-output"}
        debug = summarize_chat_response_envelope(response)
        assert debug["has_output"] is True
        assert debug["output_type"] == "str"

    def test_standard_choices_omits_top_level_error_detail(self) -> None:
        response = {
            "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
        }
        debug = summarize_chat_response_envelope(response)
        assert debug["choice_count"] == 1
        assert "top_level_keys" not in debug


class TestSanitizeModelResponseText:
    @pytest.mark.parametrize(
        ("raw", "secret", "context"),
        [
            ("config SOME_TOKEN=abc12345 ok", "abc12345", "SOME_TOKEN"),
            ("set API_KEY: xyz98765", "xyz98765", "API_KEY"),
            ('{"password": "p@ssw0rd", "summary": "x"}', "p@ssw0rd", "password"),
            ("Authorization: Bearer token-value", "token-value", "Bearer ***"),
            ("Authorization: Basic basic-secret", "basic-secret", "Basic ***"),
            ('{"authorization": "json-secret", "summary": "x"}', "json-secret", "authorization"),
            ("Authorization: plain-auth-token", "plain-auth-token", "Authorization"),
        ],
    )
    def test_sanitize_redacts_sensitive_key_values(self, raw: str, secret: str, context: str) -> None:
        redacted = sanitize_model_response_text(raw)
        assert secret not in redacted
        assert context in redacted
        assert "***" in redacted

    def test_preview_model_response_truncates_after_redaction(self) -> None:
        raw = "x" * 800 + " SOME_TOKEN=abc12345"
        preview = preview_model_response(raw, max_len=200)
        assert "abc12345" not in preview
        assert "[truncated]" in preview
        assert len(preview) <= 250


class TestContextWindowContracts:
    @pytest.mark.asyncio
    async def test_successful_tool_result_lifecycle_and_observation(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = _ObservationRecorder()
        monkeypatch.setattr(deepseek_module, "get_observability", lambda: recorder)
        captured_requests: list[dict] = []
        responses = [
            _tool_call_response(1),
            _tool_call_response(2),
            _tool_call_response(3),
            _completed_response(),
        ]

        class Registry(AgentToolRegistry):
            def __init__(self, executor) -> None:
                super().__init__(executor)
                self.index = 0

            async def execute(self, tool_name: str, arguments: dict, *, tool_call_id: str):
                self.index += 1
                return {
                    "status": "completed",
                    "success": True,
                    "id": f"result-{self.index}",
                    "path": "src/a.py",
                    "sha256": f"sha-{self.index}",
                    "cursor": f"cursor-{self.index}",
                    "exit_code": 0,
                    "unknown_payload": f"unknown-{self.index}-" + "x" * 300,
                }

        class Client:
            async def chat_completion(self, **kwargs):
                captured_requests.append(copy.deepcopy(kwargs))
                return responses[len(captured_requests) - 1]

        base_registry = _registry(test_workspace)
        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=Registry(base_registry._executor),
        )
        result = await provider.generate(_ctx(test_workspace))

        assert result.terminal_status == "completed"
        full_result = next(
            item for item in captured_requests[1]["messages"]
            if item.get("tool_call_id") == "call-1"
        )
        first_receipt = next(
            item for item in captured_requests[2]["messages"]
            if item.get("tool_call_id") == "call-1"
        )
        second_receipt = next(
            item for item in captured_requests[3]["messages"]
            if item.get("tool_call_id") == "call-1"
        )
        full_payload = json.loads(full_result["content"])
        receipt_payload = json.loads(first_receipt["content"])
        assert full_payload["unknown_payload"].startswith("unknown-1-")
        assert receipt_payload == {
            "cursor": "cursor-1",
            "exit_code": 0,
            "id": "result-1",
            "path": "src/a.py",
            "sha256": "sha-1",
            "status": "completed",
            "success": True,
            "tool_name": "report_blocked",
        }
        assert first_receipt["content"] == second_receipt["content"]
        assert "unknown_payload" not in receipt_payload
        assert recorder.generations[1]["input_summary"]["messages"] == captured_requests[1]["messages"]
        assert "unknown-1-" in json.dumps(
            recorder.generations[1]["input_summary"],
            ensure_ascii=False,
        )

    @pytest.mark.asyncio
    async def test_failed_tool_result_lifecycle_and_observation(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = _ObservationRecorder()
        monkeypatch.setattr(deepseek_module, "get_observability", lambda: recorder)
        captured_requests: list[dict] = []
        responses = [
            _tool_call_response(1),
            _tool_call_response(2),
            _tool_call_response(3),
            _completed_response(),
        ]
        long_message = "concise failure " + "z" * 2_000

        class Registry(AgentToolRegistry):
            def __init__(self, executor) -> None:
                super().__init__(executor)
                self.index = 0

            async def execute(self, tool_name: str, arguments: dict, *, tool_call_id: str):
                self.index += 1
                return {
                    "status": "failed",
                    "success": False,
                    "error_code": "command_failed",
                    "error_type": "ProcessError",
                    "exit_code": 17,
                    "category": "command",
                    "retryable": False,
                    "message": long_message,
                    "unknown_payload": f"failed-unknown-{self.index}-" + "y" * 300,
                }

        class Client:
            async def chat_completion(self, **kwargs):
                captured_requests.append(copy.deepcopy(kwargs))
                return responses[len(captured_requests) - 1]

        base_registry = _registry(test_workspace)
        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=Registry(base_registry._executor),
        )
        await provider.generate(_ctx(test_workspace))

        full_result = next(
            item for item in captured_requests[1]["messages"]
            if item.get("tool_call_id") == "call-1"
        )
        first_receipt = next(
            item for item in captured_requests[2]["messages"]
            if item.get("tool_call_id") == "call-1"
        )
        second_receipt = next(
            item for item in captured_requests[3]["messages"]
            if item.get("tool_call_id") == "call-1"
        )
        full_payload = json.loads(full_result["content"])
        receipt_payload = json.loads(first_receipt["content"])
        assert full_payload["message"] == long_message
        assert full_payload["unknown_payload"].startswith("failed-unknown-1-")
        assert receipt_payload == {
            "category": "command",
            "error_code": "command_failed",
            "error_summary": long_message[:240],
            "error_type": "ProcessError",
            "exit_code": 17,
            "retryable": False,
            "status": "failed",
            "success": False,
            "tool_name": "report_blocked",
        }
        assert first_receipt["content"] == second_receipt["content"]
        assert len(first_receipt["content"].encode("utf-8")) <= 512
        assert "unknown_payload" not in receipt_payload
        assert recorder.generations[1]["input_summary"]["messages"] == captured_requests[1]["messages"]
        assert "failed-unknown-1-" in json.dumps(
            recorder.generations[1]["input_summary"],
            ensure_ascii=False,
        )
        assert long_message in json.dumps(
            recorder.generations[1]["input_summary"],
            ensure_ascii=False,
        )

    @pytest.mark.asyncio
    async def test_invalid_and_empty_terminal_receive_repair_feedback(
        self,
        test_workspace: Path,
    ) -> None:
        async def run_repair(initial_content) -> tuple[AgentProposalSchema, list[list[dict]]]:
            captured: list[list[dict]] = []
            responses = [
                {
                    "choices": [{
                        "message": {"role": "assistant", "content": initial_content},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                },
                _completed_response(summary="fixed"),
            ]

            class Client:
                async def chat_completion(self, **kwargs):
                    captured.append(copy.deepcopy(kwargs["messages"]))
                    return responses[len(captured) - 1]

            provider = DeepSeekAgentProvider(
                client=Client(),
                model="deepseek-chat",
                registry=_registry(test_workspace),
            )
            return await provider.generate(_ctx(test_workspace)), captured

        for initial in ("not-json", None, "", "   "):
            proposal, captured = await run_repair(initial)
            assert proposal.terminal_status == "completed"
            assert proposal.summary == "fixed"
            assert len(captured) == 2
            assert captured[1][-1]["role"] == "user"
            assert "terminal_status" in captured[1][-1]["content"]

        blocked, _ = await run_repair(json.dumps({
            "terminal_status": "blocked",
            "reason": "dependency unavailable",
            "summary": "cannot continue",
            "file_patches": [],
            "tests_to_run": [],
        }))
        assert blocked.terminal_status == "blocked"
        assert blocked.reason == "dependency unavailable"

    @pytest.mark.asyncio
    async def test_model_requests_keep_full_local_observation(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = _ObservationRecorder()
        events: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(deepseek_module, "get_observability", lambda: recorder)
        monkeypatch.setattr(
            deepseek_module,
            "log_event",
            lambda action, status, **kwargs: events.append((action, status, kwargs)),
        )

        memory_requests: list[dict] = []

        class MemoryClient:
            async def chat_completion(self, **kwargs):
                memory_requests.append(copy.deepcopy(kwargs))
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": "optimized memory"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 3},
                }

        memory_provider = DeepSeekAgentProvider(
            client=MemoryClient(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        await memory_provider.optimize_memory(
            "prior summary",
            [{"role": "user", "content": "evicted original"}],
        )

        conversation_requests: list[dict] = []

        class Registry(AgentToolRegistry):
            async def execute(self, tool_name: str, arguments: dict, *, tool_call_id: str):
                return {
                    "status": "completed",
                    "success": True,
                    "id": "observed-result",
                    "unknown_payload": "full-observed-payload",
                }

        class ConversationClient:
            async def chat_completion(self, **kwargs):
                conversation_requests.append(copy.deepcopy(kwargs))
                return (
                    _tool_call_response(1)
                    if len(conversation_requests) == 1
                    else _completed_response()
                )

        base_registry = _registry(test_workspace)
        conversation_provider = DeepSeekAgentProvider(
            client=ConversationClient(),
            model="deepseek-chat",
            registry=Registry(base_registry._executor),
        )
        await conversation_provider.generate(_ctx(test_workspace))

        assert len(recorder.generations) == 3
        memory_generation = recorder.generations[0]
        assert memory_generation["name"] == "memory.optimizer"
        assert memory_generation["input_summary"] == {
            "messages": memory_requests[0]["messages"],
            "tools": [],
            "messages_count": 2,
            "tools_count": 0,
        }
        assert memory_generation["output_summary"]["choices"][0]["message"]["content"] == "optimized memory"
        assert "full-observed-payload" in json.dumps(
            recorder.generations[2]["input_summary"],
            ensure_ascii=False,
        )
        actions = {action for action, _, _ in events}
        assert {
            "deepseek_memory_optimizer",
            "deepseek_tool_result_consumed",
            "deepseek_tool_result_replaced",
            "deepseek_final_proposal_parsed",
            "deepseek_request_completed",
        } <= actions

    @pytest.mark.asyncio
    async def test_cancelled_generation_logs_and_propagates(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        events: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            deepseek_module,
            "log_event",
            lambda action, status, **kwargs: events.append((action, status, kwargs)),
        )

        class Client:
            async def chat_completion(self, **kwargs):
                started.set()
                await release.wait()
                return _completed_response()

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        task = asyncio.create_task(provider.generate(_ctx(test_workspace)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert any(
            action == "deepseek_request_cancelled"
            and status == "failed"
            and kwargs["detail"]["error_code"] == "cancelled"
            for action, status, kwargs in events
        )
        assert not any(action == "deepseek_request_completed" for action, _, _ in events)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blocked_stage", ["model_request", "runtime_tool"])
    async def test_wall_watchdog_cancels_blocking_model_and_runtime_tool_awaits(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        blocked_stage: str,
    ) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        request_timeouts: list[float] = []
        events: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            deepseek_module,
            "log_event",
            lambda action, status, **kwargs: events.append((action, status, kwargs)),
        )

        class Client:
            async def chat_completion(self, **kwargs):
                request_timeouts.append(kwargs["timeout_seconds"])
                if blocked_stage == "model_request":
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        cancelled.set()
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "blocking-runtime-tool",
                                "type": "function",
                                "function": {
                                    "name": "read_project_map",
                                    "arguments": json.dumps({"mode": "overview"}),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {},
                }

        async def blocking_runtime_handler(_arguments: dict) -> dict:
            entered.set()
            try:
                await release.wait()
            finally:
                cancelled.set()
            return {"status": "success"}

        context = _ctx(test_workspace)
        registry = AgentToolRegistry.from_context(
            context,
            runtime_handlers={"read_project_map": blocking_runtime_handler},
        )
        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_wall_seconds=1.0,
            timeout_seconds=17,
            registry=registry,
        )
        started_at = asyncio.get_running_loop().time()
        with pytest.raises(DeepSeekProviderError) as exc:
            await asyncio.wait_for(provider.generate(context), timeout=2.0)
        elapsed = asyncio.get_running_loop().time() - started_at

        assert 0.8 <= elapsed < 1.8
        assert entered.is_set()
        assert cancelled.is_set()
        assert request_timeouts == [17]
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "wall_seconds"
        assert any(
            action == "deepseek_final_proposal_invalid"
            and status == "failed"
            and kwargs["detail"]["error_code"] == "tool_budget_exhausted"
            for action, status, kwargs in events
        )

    @pytest.mark.asyncio
    async def test_wall_watchdog_uses_one_absolute_deadline_across_awaits(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler_entered = asyncio.Event()
        handler_cancelled = asyncio.Event()
        release = asyncio.Event()
        request_timeouts: list[float] = []
        events: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            deepseek_module,
            "log_event",
            lambda action, status, **kwargs: events.append((action, status, kwargs)),
        )

        class Client:
            async def chat_completion(self, **kwargs):
                request_timeouts.append(kwargs["timeout_seconds"])
                await asyncio.sleep(0.65)
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "shared-wall-deadline",
                                "type": "function",
                                "function": {
                                    "name": "read_project_map",
                                    "arguments": json.dumps({"mode": "overview"}),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {},
                }

        async def blocking_runtime_handler(_arguments: dict) -> dict:
            handler_entered.set()
            try:
                await release.wait()
            finally:
                handler_cancelled.set()
            return {"status": "success"}

        context = _ctx(test_workspace)
        registry = AgentToolRegistry.from_context(
            context,
            runtime_handlers={"read_project_map": blocking_runtime_handler},
        )
        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_wall_seconds=1.0,
            timeout_seconds=29,
            registry=registry,
        )
        started_at = asyncio.get_running_loop().time()
        with pytest.raises(DeepSeekProviderError) as exc:
            await asyncio.wait_for(provider.generate(context), timeout=1.8)
        elapsed = asyncio.get_running_loop().time() - started_at

        assert 0.85 <= elapsed < 1.4
        assert handler_entered.is_set()
        assert handler_cancelled.is_set()
        assert request_timeouts == [29]
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "wall_seconds"
        assert any(
            action == "deepseek_final_proposal_invalid"
            and status == "failed"
            and kwargs["detail"]["error_code"] == "tool_budget_exhausted"
            for action, status, kwargs in events
        )

    @pytest.mark.asyncio
    async def test_memory_optimizer_sends_no_tools(self, test_workspace: Path) -> None:
        captured: list[dict] = []

        class Client:
            async def chat_completion(self, **kwargs):
                captured.append(kwargs)
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": "optimized memory"},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        result = await provider.optimize_memory(
            "prior summary",
            [{"role": "user", "content": "evicted only"}],
        )

        assert result == "optimized memory"
        assert len(captured) == 1
        assert not captured[0].get("tools")
        rendered = json.dumps(captured[0]["messages"])
        assert "prior summary" in rendered
        assert "evicted only" in rendered

    @pytest.mark.asyncio
    async def test_tool_results_are_full_once_then_replaced_by_receipt(
        self,
        test_workspace: Path,
    ) -> None:
        completed = json.dumps({
            "terminal_status": "completed",
            "reason": "",
            "summary": "done",
            "file_patches": [],
            "tests_to_run": [],
        })
        tool_rounds = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {
                                "name": "report_blocked",
                                "arguments": json.dumps({"reason": f"round-{index}"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {},
            }
            for index in (1, 2)
        ]
        responses = [
            *tool_rounds,
            {
                "choices": [{
                    "message": {"role": "assistant", "content": completed},
                    "finish_reason": "stop",
                }],
                "usage": {},
            },
        ]

        class Registry(AgentToolRegistry):
            def __init__(self, executor) -> None:
                super().__init__(executor)
                self.result_index = 0

            async def execute(self, tool_name: str, arguments: dict, *, tool_call_id: str):
                self.result_index += 1
                return {
                    "status": "completed",
                    "success": True,
                    "id": f"result-{self.result_index}",
                    "path": "src/a.py",
                    "sha256": f"hash-{self.result_index}",
                    "payload": "large-unknown-value-" * 100,
                }

        captured: list[list[dict]] = []

        class Client:
            def __init__(self) -> None:
                self.index = 0

            async def chat_completion(self, **kwargs):
                captured.append(json.loads(json.dumps(kwargs["messages"])))
                response = responses[self.index]
                self.index += 1
                return response

        base_registry = _registry(test_workspace)
        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=Registry(base_registry._executor),
        )
        await provider.generate(_ctx(test_workspace))

        second_tools = [item for item in captured[1] if item.get("role") == "tool"]
        third_tools = [item for item in captured[2] if item.get("role") == "tool"]
        assert "large-unknown-value" in second_tools[0]["content"]
        assert "large-unknown-value" not in third_tools[0]["content"]
        assert json.loads(third_tools[0]["content"])["id"] == "result-1"
        assert "large-unknown-value" in third_tools[1]["content"]

    @pytest.mark.asyncio
    async def test_invalid_terminal_receives_repair_feedback_then_completed(
        self,
        test_workspace: Path,
    ) -> None:
        responses = [
            json.dumps({"summary": "missing terminal", "file_patches": [], "tests_to_run": []}),
            json.dumps({
                "terminal_status": "completed",
                "reason": "",
                "summary": "fixed",
                "file_patches": [],
                "tests_to_run": [],
            }),
        ]
        captured: list[list[dict]] = []

        class Client:
            async def chat_completion(self, **kwargs):
                captured.append(json.loads(json.dumps(kwargs["messages"])))
                content = responses[len(captured) - 1]
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        result = await provider.generate(_ctx(test_workspace))

        assert result.terminal_status == "completed"
        assert result.summary == "fixed"
        assert len(captured) == 2
        assert captured[1][-2] == {
            "role": "assistant",
            "content": responses[0],
        }
        repair_feedback = captured[1][-1]
        assert repair_feedback["role"] == "user"
        assert "terminal_status" in repair_feedback["content"]
        assert "completed" in repair_feedback["content"]
        assert "blocked" in repair_feedback["content"]

    @pytest.mark.asyncio
    async def test_blocked_terminal_returns_reason(self, test_workspace: Path) -> None:
        blocked = json.dumps({
            "terminal_status": "blocked",
            "reason": "dependency unavailable",
            "summary": "cannot continue",
            "file_patches": [],
            "tests_to_run": [],
        })

        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": blocked},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            registry=_registry(test_workspace),
        )
        result = await provider.generate(_ctx(test_workspace))

        assert result.terminal_status == "blocked"
        assert result.reason == "dependency unavailable"

