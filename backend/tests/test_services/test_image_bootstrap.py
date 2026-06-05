"""Tests for ImageBootstrapService."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bridle.services.image_bootstrap import (
    REQUIRED_IMAGES,
    ImageBootstrapError,
    ImageBootstrapService,
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    for name in ("main-agent.Dockerfile", "node-agent.Dockerfile"):
        (docker_dir / name).write_text("# test stub\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def service(repo_root: Path) -> ImageBootstrapService:
    logs: list[str] = []
    svc = ImageBootstrapService(repo_root, log=logs.append)
    svc._logs = logs  # type: ignore[attr-defined]
    return svc


class TestImageBootstrapDaemon:
    def test_daemon_down_raises(self, service: ImageBootstrapService) -> None:
        with patch("bridle.services.image_bootstrap.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stderr="Cannot connect")
            with pytest.raises(ImageBootstrapError) as exc_info:
                service.ensure_ready()
        assert exc_info.value.code == "docker_daemon_unavailable"
        assert "Docker Desktop" in str(exc_info.value)


class TestImageBootstrapImagesPresent:
    def test_existing_images_skip_build(self, service: ImageBootstrapService) -> None:
        with patch("bridle.services.image_bootstrap.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            service.ensure_ready()
        # docker info + 2x image inspect only
        assert run.call_count == 1 + len(REQUIRED_IMAGES)
        build_calls = [
            c for c in run.call_args_list if c.args and c.args[0][:2] == ("docker", "build")
        ]
        assert build_calls == []


class TestImageBootstrapMissingImage:
    def test_missing_image_triggers_build_and_logs(
        self, service: ImageBootstrapService
    ) -> None:
        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:2] == ["docker", "info"]:
                return MagicMock(returncode=0)
            if cmd[:3] == ["docker", "image", "inspect"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["Step 1/5\n", "Successfully built\n"])
        mock_proc.wait.return_value = 0

        with patch("bridle.services.image_bootstrap.subprocess.run", side_effect=run_side_effect):
            with patch("bridle.services.image_bootstrap.subprocess.Popen", return_value=mock_proc) as popen:
                service.ensure_ready()

        assert popen.call_count == len(REQUIRED_IMAGES)
        logs: list[str] = service._logs  # type: ignore[attr-defined]
        assert any("Step 1/5" in line for line in logs)


class TestImageBootstrapBuildFailure:
    def test_build_nonzero_exit_raises(self, service: ImageBootstrapService) -> None:
        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:2] == ["docker", "info"]:
                return MagicMock(returncode=0)
            if cmd[:3] == ["docker", "image", "inspect"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["ERROR: build failed\n"])
        mock_proc.wait.return_value = 1

        with patch("bridle.services.image_bootstrap.subprocess.run", side_effect=run_side_effect):
            with patch("bridle.services.image_bootstrap.subprocess.Popen", return_value=mock_proc):
                with pytest.raises(ImageBootstrapError) as exc_info:
                    service.ensure_ready()
        assert exc_info.value.code == "image_build_failed"
        assert "build failed" in str(exc_info.value).lower() or "ERROR" in str(exc_info.value)


class TestImageBootstrapImageInspectTimeout:
    def test_inspect_timeout_treated_as_missing(self, service: ImageBootstrapService) -> None:
        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:2] == ["docker", "info"]:
                return MagicMock(returncode=0)
            if cmd[:3] == ["docker", "image", "inspect"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)
            return MagicMock(returncode=0)

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["ok\n"])
        mock_proc.wait.return_value = 0

        with patch("bridle.services.image_bootstrap.subprocess.run", side_effect=run_side_effect):
            with patch("bridle.services.image_bootstrap.subprocess.Popen", return_value=mock_proc) as popen:
                service.ensure_ready()

        assert popen.call_count == len(REQUIRED_IMAGES)


class TestImageBootstrapForceRebuild:
    def test_force_rebuild_builds_even_when_present(self, service: ImageBootstrapService) -> None:
        mock_proc = MagicMock()
        mock_proc.stdout = iter(["building\n"])
        mock_proc.wait.return_value = 0

        with patch("bridle.services.image_bootstrap.subprocess.run", return_value=MagicMock(returncode=0)):
            with patch("bridle.services.image_bootstrap.subprocess.Popen", return_value=mock_proc) as popen:
                service.ensure_ready(force_rebuild=True)

        assert popen.call_count == len(REQUIRED_IMAGES)
