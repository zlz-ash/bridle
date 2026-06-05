"""Tests for bridle serve image bootstrap flags."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from bridle.cli import app
from bridle.services.image_bootstrap import ImageBootstrapError


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_serve_side_effects() -> Iterator[None]:
    """``serve`` sets ``os.environ`` directly; pop after each test."""
    import bridle.config as cfg

    os.environ.pop("BRIDLE_WORKSPACE", None)
    cfg._global_config = None
    yield
    os.environ.pop("BRIDLE_WORKSPACE", None)
    cfg._global_config = None


@contextmanager
def _serve_context(mock_svc: MagicMock | None = None):
    """Patches so ``serve`` reaches bootstrap then uvicorn without real DB/docker."""
    svc = mock_svc if mock_svc is not None else MagicMock()
    with (
        patch("bridle.services.image_bootstrap.ImageBootstrapService", return_value=svc),
        patch("uvicorn.run"),
        patch("asyncio.run", side_effect=lambda coro: None),
        patch("bridle.cli._load_env_files", return_value=[]),
        patch("bridle.config.set_workspace"),
        patch("bridle.models", create=True),
        patch("bridle.database._ensure_engine"),
        patch("bridle.database._engine") as eng_patch,
    ):
        conn = MagicMock()
        eng_patch.begin.return_value.__aenter__ = AsyncMock(return_value=conn)
        eng_patch.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        try:
            yield svc
        finally:
            os.environ.pop("BRIDLE_WORKSPACE", None)


class TestServeImageBootstrapCli:
    def test_default_calls_ensure_ready(self, runner: CliRunner, workspace: Path) -> None:
        mock_svc = MagicMock()
        with _serve_context(mock_svc):
            runner.invoke(
                app,
                ["serve", "--workspace", str(workspace)],
                catch_exceptions=False,
            )
        mock_svc.ensure_ready.assert_called_once_with(force_rebuild=False)

    def test_rebuild_images_passes_force_true(self, runner: CliRunner, workspace: Path) -> None:
        mock_svc = MagicMock()
        with _serve_context(mock_svc):
            runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--rebuild-images"],
                catch_exceptions=False,
            )
        mock_svc.ensure_ready.assert_called_once_with(force_rebuild=True)

    def test_skip_image_build_does_not_instantiate_service(
        self, runner: CliRunner, workspace: Path
    ) -> None:
        with (
            patch("bridle.services.image_bootstrap.ImageBootstrapService") as SvcCls,
            patch("uvicorn.run"),
            patch("asyncio.run", side_effect=lambda coro: None),
            patch("bridle.cli._load_env_files", return_value=[]),
            patch("bridle.config.set_workspace"),
            patch("bridle.models", create=True),
            patch("bridle.database._ensure_engine"),
            patch("bridle.database._engine") as eng_patch,
        ):
            eng_patch.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            eng_patch.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--skip-image-build"],
                catch_exceptions=False,
            )
        SvcCls.assert_not_called()

    def test_bootstrap_error_exits_code_2(self, runner: CliRunner, workspace: Path) -> None:
        mock_svc = MagicMock()
        mock_svc.ensure_ready.side_effect = ImageBootstrapError(
            "docker_daemon_unavailable",
            "请先启动 Docker Desktop，等托盘鲸鱼变绿后重试。",
        )
        with (
            patch("bridle.services.image_bootstrap.ImageBootstrapService", return_value=mock_svc),
            patch("uvicorn.run") as uvicorn_run,
            patch("bridle.cli._load_env_files", return_value=[]),
            patch("bridle.config.set_workspace"),
        ):
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace)],
            )
        assert result.exit_code == 2
        assert "docker_daemon_unavailable" in (result.stderr + result.output)
        uvicorn_run.assert_not_called()
