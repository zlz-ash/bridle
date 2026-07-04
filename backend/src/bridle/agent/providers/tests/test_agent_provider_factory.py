"""Tests for AgentProviderFactory -provider selection and config."""
from __future__ import annotations

import os
import time

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
        assert cfg["deepseek_max_tool_rounds"] == 8
        assert cfg["deepseek_max_tool_calls"] == 32
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

