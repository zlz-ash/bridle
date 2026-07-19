"""Tests for AgentProviderFactory -provider selection and config."""
from __future__ import annotations

import pytest


class TestAgentProviderFactory:
    """Unit tests for provider factory."""

    def _clean_env(self, monkeypatch):
        for var in (
            "BRIDLE_AGENT_PROVIDER",
            "BRIDLE_AGENT_MODEL",
            "BRIDLE_AGENT_API_KEY",
            "BRIDLE_AGENT_TIMEOUT_SECONDS",
            "HTTPS_PROXY",
            "BRIDLE_DEEPSEEK_STRICT_TOOLS",
            "BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_defaults_to_fake(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        provider = AgentProviderFactory.create()
        assert provider.name == "fake"

    def test_no_api_key_falls_back_to_fake(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "configured_stub")
        # No API key set
        provider = AgentProviderFactory.create()
        assert provider.name == "fake"

    def test_full_config_uses_stub(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "configured_stub")
        monkeypatch.setenv("BRIDLE_AGENT_API_KEY", "sk-test-key")
        provider = AgentProviderFactory.create()
        assert provider.name == "configured_stub"

    def test_default_proxy_is_local_7890(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        cfg = AgentProviderFactory.get_config()
        assert cfg["proxy"] == "http://127.0.0.1:7890"

    def test_custom_proxy(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        monkeypatch.setenv("HTTPS_PROXY", "http://custom:3128")
        cfg = AgentProviderFactory.get_config()
        assert cfg["proxy"] == "http://custom:3128"

    def test_default_timeout(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        cfg = AgentProviderFactory.get_config()
        assert cfg["timeout_seconds"] == 120

    def test_custom_timeout(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        monkeypatch.setenv("BRIDLE_AGENT_TIMEOUT_SECONDS", "30")
        cfg = AgentProviderFactory.get_config()
        assert cfg["timeout_seconds"] == 30

    def test_respects_model_env(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        monkeypatch.setenv("BRIDLE_AGENT_MODEL", "gpt-5-mini")
        cfg = AgentProviderFactory.get_config()
        assert cfg["model"] == "gpt-5-mini"

    def test_deepseek_without_api_key_falls_back(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "deepseek")
        provider = AgentProviderFactory.create()
        assert provider.name == "fake"

    def test_deepseek_with_key_and_context(self, monkeypatch, test_workspace) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory
        from bridle.agent.runtime.schemas import AgentContext

        self._clean_env(monkeypatch)
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "deepseek")
        monkeypatch.setenv("BRIDLE_AGENT_API_KEY", "sk-secret-key")
        monkeypatch.setenv("BRIDLE_AGENT_MODEL", "deepseek-chat")
        ctx = AgentContext(
            instruction="x",
            node={"id": "n1"},
            allowed_files=[],
            tests=[],
            metrics={},
            constraints={},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
            tool_capabilities={
                "sandbox": {
                    "run_id": "r",
                    "node_id": "n",
                    "workspace_root": str(test_workspace),
                    "allowed_files": [],
                    "allowed_test_commands": [],
                },
            },
        )
        provider = AgentProviderFactory.create(context=ctx)
        assert provider.name == "deepseek"

    def test_deepseek_config_defaults(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        cfg = AgentProviderFactory.get_config()
        assert cfg["deepseek_strict_tools"] is False
        assert cfg["deepseek_max_wall_seconds"] == 300.0

    def test_no_api_key_in_logs_on_fallback(self, monkeypatch, caplog) -> None:
        import logging

        from bridle.agent.providers.agent_provider import AgentProviderFactory

        self._clean_env(monkeypatch)
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "deepseek")
        caplog.set_level(logging.WARNING)
        AgentProviderFactory.create()
        assert "sk-" not in caplog.text
        assert "secret" not in caplog.text.lower()


class TestConfiguredStubProvider:
    """Tests for the configured stub provider used in pre-real-LLM config validation."""

    @pytest.mark.asyncio
    async def test_generates_proposal(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import ConfiguredStubProvider
        from bridle.agent.runtime.schemas import AgentContext

        provider = ConfiguredStubProvider(model="test-model", timeout_seconds=10, proxy=None)
        ctx = AgentContext(
            instruction="Test stub",
            node={"id": "n1"},
            allowed_files=["src/a.py"],
            tests=[],
            metrics={},
            constraints={},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
        )
        result = await provider.generate(ctx)
        assert result.summary != ""
        assert isinstance(result.file_patches, list)
        assert isinstance(result.tests_to_run, list)


    @pytest.mark.asyncio
    async def test_respects_timeout(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import ConfiguredStubProvider
        from bridle.agent.runtime.schemas import AgentContext

        provider = ConfiguredStubProvider(model="slow-model", timeout_seconds=0.001, proxy=None)
        ctx = AgentContext(
            instruction="Test timeout",
            node={"id": "n1"},
            allowed_files=["src/a.py"],
            tests=[],
            metrics={},
            constraints={},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
        )
        # Should still work -stub doesn't actually timeout on valid input
        result = await provider.generate(ctx)
        assert result.summary != ""

    @pytest.mark.asyncio
    async def test_same_input_produces_same_output(self, monkeypatch) -> None:
        from bridle.agent.providers.agent_provider import ConfiguredStubProvider
        from bridle.agent.runtime.schemas import AgentContext

        provider = ConfiguredStubProvider(model="m1", timeout_seconds=10, proxy=None)
        ctx = AgentContext(
            instruction="Idempotent test",
            node={"id": "n1"},
            allowed_files=["a.py"],
            tests=[],
            metrics={},
            constraints={},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
        )
        r1 = await provider.generate(ctx)
        r2 = await provider.generate(ctx)
        assert r1.model_dump() == r2.model_dump()


def test_factory_and_node_budget_ignore_legacy_round_call_limits(
    monkeypatch,
    test_workspace,
) -> None:
    from bridle.agent.providers import deepseek_agent_provider as deepseek_module
    from bridle.agent.providers import openai_client as openai_client_module
    from bridle.agent.providers.agent_provider import AgentProviderFactory
    from bridle.agent.runtime.authorization import BudgetGrant
    from bridle.agent.runtime.schemas import AgentContext
    from bridle.agent.tools.budget import budget_for_node_minutes

    monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "deepseek")
    monkeypatch.setenv("BRIDLE_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS", "1")
    monkeypatch.setenv("BRIDLE_DEEPSEEK_MAX_TOOL_CALLS", "1")
    captured: dict = {}

    class DummyClient:
        def __init__(self, **kwargs) -> None:
            captured["client"] = kwargs

    class DummyProvider:
        name = "deepseek"

        def __init__(self, **kwargs) -> None:
            captured["provider"] = kwargs

    monkeypatch.setattr(openai_client_module, "HttpOpenAICompatibleClient", DummyClient)
    monkeypatch.setattr(deepseek_module, "DeepSeekAgentProvider", DummyProvider)
    context = AgentContext(
        instruction="x",
        node={"id": "n1"},
        tool_capabilities={
            "sandbox": {
                "run_id": "r1",
                "node_id": "n1",
                "workspace_root": str(test_workspace),
                "allowed_files": [],
                "node_tests": [],
            }
        },
    )

    AgentProviderFactory.create(
        context,
        budget_override={
            "max_rounds": 1,
            "max_tool_calls": 1,
            "max_wall_seconds": 42,
        },
    )

    assert set(budget_for_node_minutes(30)) == {"max_wall_seconds"}
    assert "deepseek_max_tool_rounds" not in AgentProviderFactory.get_config()
    assert "deepseek_max_tool_calls" not in AgentProviderFactory.get_config()
    assert "max_tool_rounds" not in captured["provider"]
    assert "max_tool_calls" not in captured["provider"]
    assert captured["provider"]["max_wall_seconds"] == 42
    assert BudgetGrant(max_tool_calls=3).max_tool_calls == 3

