"""Provider-level logging boundaries — structured events without secrets."""
from __future__ import annotations

import logging

import pytest


class TestProposalProviderLogging:
    """Aligned with PLAN.md logging tests (started/failed/no API key leakage)."""

    def test_provider_event_logged_with_action_message(self, caplog: pytest.LogCaptureFixture) -> None:
        from bridle.services.agent_gateway import _log_provider_event

        caplog.set_level(logging.INFO, logger="bridle")
        _log_provider_event(
            "proposal_provider_started",
            "started",
            node_id="nid-a",
            plan_node_id="pn-1",
            provider="fake",
            model="m1",
        )
        assert any(rec.message == "proposal_provider_started" for rec in caplog.records)

    def test_provider_completed_event_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from bridle.services.agent_gateway import _log_provider_event

        caplog.set_level(logging.INFO, logger="bridle")
        _log_provider_event(
            "proposal_provider_completed",
            "completed",
            node_id="nid-b",
            plan_node_id="pn-2",
            provider="configured_stub",
            model="gpt-test",
            duration_ms=42,
        )
        completed = [r for r in caplog.records if r.message == "proposal_provider_completed"]
        assert len(completed) == 1
        assert completed[0].status == "completed"  # type: ignore[attr-defined]

    def test_provider_failed_event_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from bridle.services.agent_gateway import _log_provider_event

        caplog.set_level(logging.INFO, logger="bridle")
        _log_provider_event(
            "proposal_provider_failed",
            "failed",
            node_id="nid-c",
            plan_node_id="pn-3",
            provider="fake",
            model="unknown",
            duration_ms=100,
            error_code="timeout",
        )
        failed = [r for r in caplog.records if r.message == "proposal_provider_failed"]
        assert len(failed) == 1
        assert failed[0].status == "failed"  # type: ignore[attr-defined]

    def test_boundary_rejected_event_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from bridle.services.agent_gateway import _log_provider_event

        caplog.set_level(logging.INFO, logger="bridle")
        _log_provider_event(
            "proposal_boundary_rejected",
            "rejected",
            node_id="nid-d",
            plan_node_id="pn-4",
            provider="fake",
            model="unknown",
            duration_ms=5,
            error_code="PathBoundaryError",
            detail_str="Patch [0]: path '../secret.py' contains parent traversal '..'",
        )
        assert any(rec.message == "proposal_boundary_rejected" for rec in caplog.records)

    def test_unknown_provider_fallback_log_never_contains_api_key(self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        from bridle.engine.agent_provider import AgentProviderFactory

        secret = "sk-super-secret-key-do-not-log-999"
        monkeypatch.delenv("BRIDLE_AGENT_PROVIDER", raising=False)
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "bogus_xyz_provider")
        monkeypatch.setenv("BRIDLE_AGENT_API_KEY", secret)

        caplog.set_level(logging.WARNING, logger="bridle")
        AgentProviderFactory.create()

        assert secret not in caplog.text
