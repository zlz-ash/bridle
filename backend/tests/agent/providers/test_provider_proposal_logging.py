"""Provider-level logging boundaries -structured events without secrets."""
from __future__ import annotations

import logging

import pytest


class TestProposalProviderLogging:
    """Aligned with PLAN.md logging tests (started/failed/no API key leakage)."""

    def test_unknown_provider_fallback_log_never_contains_api_key(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bridle.agent.providers.agent_provider import AgentProviderFactory

        secret = "sk-super-secret-key-do-not-log-999"
        monkeypatch.delenv("BRIDLE_AGENT_PROVIDER", raising=False)
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "bogus_xyz_provider")
        monkeypatch.setenv("BRIDLE_AGENT_API_KEY", secret)

        caplog.set_level(logging.WARNING, logger="bridle")
        AgentProviderFactory.create()

        assert secret not in caplog.text

