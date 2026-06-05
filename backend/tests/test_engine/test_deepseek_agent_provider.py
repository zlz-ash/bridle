"""Tests for DeepSeekAgentProvider with mock client."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.engine.agent_tool_registry import AgentToolRegistry
from bridle.engine.deepseek_agent_provider import (
    DeepSeekAgentProvider,
    DeepSeekProviderError,
    parse_proposal_content,
    preview_model_response,
    sanitize_model_response_text,
    summarize_chat_response_envelope,
)
from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.engine.sandboxed_tool_executor import SandboxedToolExecutor
from bridle.schemas.proposal import AgentContext


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


class TestParseProposalContent:
    def test_parses_raw_json(self) -> None:
        raw = json.dumps({
            "summary": "done",
            "file_patches": [],
            "tests_to_run": [],
        })
        p = parse_proposal_content(raw)
        assert p.summary == "done"

    def test_parses_fenced_json(self) -> None:
        raw = '```json\n{"summary":"s","file_patches":[],"tests_to_run":[]}\n```'
        p = parse_proposal_content(raw)
        assert p.summary == "s"


class TestDeepSeekAgentProvider:
    @pytest.mark.asyncio
    async def test_single_round_final_json(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
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
            max_tool_rounds=8,
            registry=_registry(test_workspace),
        )
        result = await provider.generate(_ctx(test_workspace))
        assert result.summary == "DeepSeek proposal"

    @pytest.mark.asyncio
    async def test_multi_round_tool_call_then_proposal(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
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
                                "name": "read_allowed_file",
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
            max_tool_rounds=8,
            registry=_registry(test_workspace),
        )
        client = Client()
        provider = DeepSeekAgentProvider(
            client=client,
            model="deepseek-chat",
            max_tool_rounds=8,
            registry=_registry(test_workspace),
        )
        result = await provider.generate(_ctx(test_workspace))
        assert result.summary == "after tools"
        assert client.i == 2

    @pytest.mark.asyncio
    async def test_max_tool_rounds_one_allows_single_model_request(self, test_workspace: Path) -> None:
        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c",
                        "type": "function",
                        "function": {
                            "name": "read_allowed_file",
                            "arguments": json.dumps({"path": "src/a.py"}),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }
        call_count = 0

        class Client:
            async def chat_completion(self, **kwargs):
                nonlocal call_count
                call_count += 1
                return tool_resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_tool_rounds=1,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert call_count == 1
        assert exc.value.error_code == "tool_budget_exhausted"
        last = exc.value.response_debug.get("last_tool_call") or {}
        assert last.get("tool_name") == "read_allowed_file"
        assert "src/a.py" in (last.get("args_summary") or "")

    @pytest.mark.asyncio
    async def test_budget_exhausted_last_tool_call_redacts_sensitive_args(
        self,
        test_workspace: Path,
    ) -> None:
        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "read_allowed_file",
                                "arguments": json.dumps({
                                    "path": "src/a.py",
                                    "API_KEY": "secret-value",
                                }),
                            },
                        },
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "grep_code",
                                "arguments": json.dumps({"query": "test"}),
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }

        class Client:
            async def chat_completion(self, **kwargs):
                return tool_resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_tool_rounds=20,
            max_tool_calls=1,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        last = exc.value.response_debug.get("last_tool_call") or {}
        assert last.get("tool_name") == "read_allowed_file"
        summary = last.get("args_summary") or ""
        assert "secret-value" not in summary
        assert "API_KEY" in summary or "***" in summary

    @pytest.mark.asyncio
    async def test_max_tool_rounds_exceeded(self, test_workspace: Path) -> None:
        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "c",
                        "type": "function",
                        "function": {
                            "name": "read_allowed_file",
                            "arguments": json.dumps({"path": "src/a.py"}),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }

        class Client:
            async def chat_completion(self, **kwargs):
                return tool_resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_tool_rounds=1,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "rounds"

    @pytest.mark.asyncio
    async def test_max_tool_calls_exceeded(self, test_workspace: Path) -> None:
        tool_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "read_allowed_file",
                                "arguments": json.dumps({"path": "src/a.py"}),
                            },
                        },
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "read_allowed_file",
                                "arguments": json.dumps({"path": "src/a.py"}),
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }

        class Client:
            async def chat_completion(self, **kwargs):
                return tool_resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_tool_rounds=20,
            max_tool_calls=1,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "tool_calls"

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
                            "name": "read_allowed_file",
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

        monkeypatch.setattr("bridle.engine.tool_budget.time.monotonic", fake_monotonic)
        monkeypatch.setattr("bridle.engine.deepseek_agent_provider.time.monotonic", fake_monotonic)

        class Client:
            async def chat_completion(self, **kwargs):
                return tool_resp

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="deepseek-chat",
            max_tool_rounds=20,
            max_tool_calls=50,
            max_wall_seconds=1.0,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "wall_seconds"

    @pytest.mark.asyncio
    async def test_invalid_final_json(self, test_workspace: Path) -> None:
        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": "not json"},
                        "finish_reason": "stop",
                    }],
                    "usage": {},
                }

        provider = DeepSeekAgentProvider(
            client=Client(), model="m", max_tool_rounds=4, registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "invalid_agent_proposal"

    @pytest.mark.asyncio
    async def test_out_of_boundary_patch_fails(self, test_workspace: Path) -> None:
        bad = json.dumps({
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
            client=Client(), model="m", max_tool_rounds=4, registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "PathBoundaryError"

    @pytest.mark.asyncio
    async def test_empty_allowlist_rejects_context_tests_command(self, test_workspace: Path) -> None:
        raw = json.dumps({
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
            client=Client(), model="m", max_tool_rounds=4, registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(ctx)
        assert exc.value.error_code == "CommandPolicyError"


class TestToolCircuitBreaker:
    @pytest.mark.asyncio
    async def test_failed_tool_message_includes_attempt_counters(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
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
                                "name": "read_allowed_file",
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
            max_tool_rounds=4,
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
            max_tool_rounds=5,
            registry=RetryableRegistry(_registry(test_workspace)._executor),
        )
        await provider.generate(_ctx(test_workspace))
        tool_messages = [json.loads(m["content"]) for m in client.messages_seen if m.get("role") == "tool"]
        assert tool_messages[0]["attempts"] == 1
        assert tool_messages[0]["consecutive_failures"] == 1
        assert tool_messages[1]["attempts"] == 2
        assert tool_messages[1]["consecutive_failures"] == 2

    @pytest.mark.asyncio
    async def test_success_tool_message_includes_attempt_and_resets_failures(self, test_workspace: Path) -> None:
        proposal_json = json.dumps({
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
                                "name": "read_allowed_file",
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
            max_tool_rounds=4,
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
                                    "name": "read_allowed_file",
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
            max_tool_rounds=5,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc:
            await provider.generate(_ctx(test_workspace))
        assert exc.value.error_code == "tool_budget_exhausted"
        assert exc.value.response_debug.get("budget", {}).get("type") == "rounds"

    @pytest.mark.asyncio
    async def test_circuit_breaker_returns_circuit_open(self, test_workspace: Path) -> None:
        from bridle.engine.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args = {"path": "../secret.py"}
        result1 = {"status": "failed", "error_code": "PathBoundaryError", "category": "policy", "retryable": False}
        tracker.record_result("read_allowed_file", args, result1)
        circuit = tracker.should_circuit_open("read_allowed_file", args)
        assert circuit is not None
        assert circuit["error_code"] == "tool_circuit_open"
        assert circuit["consecutive_failures"] == 1
        assert circuit["retryable"] is False

    @pytest.mark.asyncio
    async def test_different_args_no_circuit(self, test_workspace: Path) -> None:
        from bridle.engine.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args1 = {"path": "../secret.py"}
        args2 = {"path": "src/a.py"}
        result1 = {"status": "failed", "error_code": "PathBoundaryError", "category": "policy", "retryable": False}
        tracker.record_result("read_allowed_file", args1, result1)
        circuit = tracker.should_circuit_open("read_allowed_file", args2)
        assert circuit is None

    @pytest.mark.asyncio
    async def test_retryable_allows_two_attempts(self, test_workspace: Path) -> None:
        from bridle.engine.deepseek_agent_provider import ToolCallTracker

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
    async def test_run_allowed_tests_retryable_allows_second_attempt(self, test_workspace: Path) -> None:
        from bridle.engine.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args = {"commands": ["python -m pytest tests/ -q"]}
        fail = {
            "status": "failed",
            "error_code": "TestCommandFailed",
            "category": "test_failure",
            "retryable": True,
        }
        tracker.record_result("run_allowed_tests", args, fail)
        assert tracker.should_circuit_open("run_allowed_tests", args) is None

    @pytest.mark.asyncio
    async def test_run_allowed_tests_circuit_opens_at_test_command_max(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bridle.engine.deepseek_agent_provider import ToolCallTracker

        monkeypatch.setenv("BRIDLE_CIRCUIT_TEST_COMMAND_MAX", "2")
        tracker = ToolCallTracker()
        args = {"commands": ["python -m pytest tests/ -q"]}
        fail = {
            "status": "failed",
            "error_code": "TestCommandFailed",
            "category": "test_failure",
            "retryable": True,
        }
        tracker.record_result("run_allowed_tests", args, fail)
        assert tracker.should_circuit_open("run_allowed_tests", args) is None
        tracker.record_result("run_allowed_tests", args, fail)
        circuit = tracker.should_circuit_open("run_allowed_tests", args)
        assert circuit is not None
        assert circuit["error_code"] == "tool_circuit_open"

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_failures(self, test_workspace: Path) -> None:
        from bridle.engine.deepseek_agent_provider import ToolCallTracker

        tracker = ToolCallTracker()
        args = {"path": "../secret.py"}
        fail = {"status": "failed", "error_code": "PathBoundaryError", "category": "policy", "retryable": False}
        tracker.record_result("read_allowed_file", args, fail)
        success = {"status": "completed", "category": "success", "retryable": False}
        tracker.record_result("read_allowed_file", args, success)
        circuit = tracker.should_circuit_open("read_allowed_file", args)
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
            client=Client(), model="m", max_tool_rounds=4, registry=_registry(test_workspace),
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
            client=Client(), model="m", max_tool_rounds=4, registry=_registry(test_workspace),
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
                                "name": "read_allowed_file",
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
            max_tool_rounds=8,
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
                                "name": "read_allowed_file",
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
            max_tool_rounds=8,
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
    async def test_empty_content_error_includes_response_debug(self, test_workspace: Path) -> None:
        class Client:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{
                        "message": {"role": "assistant", "content": None},
                        "finish_reason": "stop",
                    }],
                    "usage": {"total_tokens": 10},
                }

        provider = DeepSeekAgentProvider(
            client=Client(),
            model="m",
            max_tool_rounds=2,
            registry=_registry(test_workspace),
        )
        with pytest.raises(DeepSeekProviderError) as exc_info:
            await provider.generate(_ctx(test_workspace))
        assert exc_info.value.error_code == "invalid_agent_proposal"
        debug = exc_info.value.response_debug
        assert debug["finish_reason"] == "stop"
        assert debug["content_is_null"] is True
        assert debug["tool_call_count"] == 0


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
