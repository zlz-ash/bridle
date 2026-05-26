"""Tests for main agent container session metadata."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.engine.container_runner import ContainerResult, FakeContainerRunner, LocalContainerRuntimeRunner
from bridle.engine.container_runner_factory import resolve_container_runner
from bridle.services.main_agent_container_service import MainAgentContainerService


class TestMainAgentContainerService:
    def test_records_metadata_after_git_preflight(self, test_workspace: Path) -> None:
        service = MainAgentContainerService(test_workspace, runner=FakeContainerRunner())
        metadata = service.record_for_session(session_id="session-1", plan_id="plan-1")

        assert metadata["session_id"] == "session-1"
        assert metadata["plan_id"] == "plan-1"
        assert metadata["container_id"] == "fake-container-1"
        assert metadata["status"] == "running"
        assert metadata["network_mode"] == "bridge"
        assert metadata["baseline_revision"] == "a" * 40

        saved = json.loads(
            (test_workspace / ".aicoding" / "main-agent-containers" / "session-1.json").read_text(encoding="utf-8")
        )
        assert saved == metadata
        assert service.read_for_session("session-1") == metadata

    def test_refuses_non_git_workspace(self, test_workspace: Path) -> None:
        import shutil
        shutil.rmtree(test_workspace / ".git", ignore_errors=True)

        service = MainAgentContainerService(test_workspace, runner=FakeContainerRunner())

        with pytest.raises(ValueError, match="not_git_repository"):
            service.record_for_session(session_id="session-1", plan_id="plan-1")

    def test_returns_none_when_metadata_missing(self, test_workspace: Path) -> None:
        service = MainAgentContainerService(test_workspace, runner=FakeContainerRunner())

        assert service.read_for_session("missing") is None

    def test_default_runner_is_not_fake_outside_test_mode(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bridle.api.deps as deps

        monkeypatch.setattr(deps, "_test_db", None)
        monkeypatch.delenv("BRIDLE_CONTAINER_RUNNER", raising=False)
        service = MainAgentContainerService(test_workspace)
        assert isinstance(service.runner, LocalContainerRuntimeRunner)

    def test_resolve_container_runner_matches_service_default(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bridle.api.deps as deps

        monkeypatch.setattr(deps, "_test_db", None)
        assert isinstance(
            resolve_container_runner(test_workspace),
            LocalContainerRuntimeRunner,
        )

    def test_detached_unhealthy_raises_valueerror(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner()

        def unhealthy_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="running",
                network_mode=current.network_mode,
                health="unhealthy",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = unhealthy_inspect
        service = MainAgentContainerService(test_workspace, runner=runner)

        with pytest.raises(ValueError, match="container_health_failed"):
            service.record_for_session(session_id="session-bad", plan_id="plan-1")

        assert service.read_for_session("session-bad") is None
