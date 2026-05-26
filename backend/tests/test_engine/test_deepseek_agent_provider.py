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
        assert exc.value.error_code == "tool_round_limit_exceeded"

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
