"""Tests for container runner factory selection."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.api import deps
from bridle.engine.container_runner import FakeContainerRunner, LocalContainerRuntimeRunner
from bridle.engine.container_runner_factory import resolve_container_runner


class TestContainerRunnerFactory:
    def test_test_mode_uses_fake_runner(self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "_test_db", object())
        runner = resolve_container_runner(test_workspace)
        assert isinstance(runner, FakeContainerRunner)

    def test_explicit_fake_env_uses_fake_runner(self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "_test_db", None)
        monkeypatch.setenv("BRIDLE_CONTAINER_RUNNER", "fake")
        runner = resolve_container_runner(test_workspace)
        assert isinstance(runner, FakeContainerRunner)

    def test_production_default_uses_local_runtime_runner(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deps, "_test_db", None)
        monkeypatch.delenv("BRIDLE_CONTAINER_RUNNER", raising=False)
        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        runner = resolve_container_runner(test_workspace)
        assert isinstance(runner, LocalContainerRuntimeRunner)

    def test_injected_runner_is_returned_unchanged(self, test_workspace: Path) -> None:
        custom = FakeContainerRunner(workspace_root=test_workspace)
        assert resolve_container_runner(test_workspace, runner=custom) is custom

    def test_production_default_is_local_runtime(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import bridle.api.deps as deps

        monkeypatch.setattr(deps, "_test_db", None)
        monkeypatch.delenv("BRIDLE_CONTAINER_RUNNER", raising=False)
        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        runner = resolve_container_runner(test_workspace)
        assert isinstance(runner, LocalContainerRuntimeRunner)
