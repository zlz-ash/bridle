"""Network boundary tests for ``bridle serve``.

The API has no auth/authorization contract, so the CLI must default to
loopback and fail-closed on non-loopback binds. These tests exercise the
real CLI entry via the Typer runner and assert:

* the default host is ``127.0.0.1`` and uvicorn receives ``reload=False``;
* ``0.0.0.0``, ``::`` and a non-loopback IP are refused with a non-zero
  exit code and a structured diagnostic;
* the bind decision (host / loopback / reason) is logged before the
  refusal so operators can see why exposure was rejected.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from bridle.cli import app


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
    import bridle.config as cfg

    os.environ.pop("BRIDLE_WORKSPACE", None)
    cfg._global_config = None
    yield
    os.environ.pop("BRIDLE_WORKSPACE", None)
    cfg._global_config = None


@contextmanager
def _serve_context():
    """Patch everything past the host check so we can isolate the boundary."""
    with (
        patch("bridle.features.workspace.git_initializer.GitWorkspaceInitializer", return_value=MagicMock()),
        patch("uvicorn.run") as uvicorn_run,
        patch("bridle.cli._load_env_files", return_value=[]),
        patch("bridle.config.set_workspace"),
        patch("bridle.models", create=True),
        patch("bridle.database._ensure_engine"),
        patch("bridle.database._engine") as eng_patch,
        patch("asyncio.run", side_effect=lambda coro: None),
    ):
        conn = MagicMock()
        eng_patch.begin.return_value.__aenter__ = AsyncMock(return_value=conn)
        eng_patch.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        try:
            yield uvicorn_run
        finally:
            os.environ.pop("BRIDLE_WORKSPACE", None)


class TestServeNetworkBoundary:
    def test_default_host_is_loopback_and_reload_disabled(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        with _serve_context() as uvicorn_run:
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        uvicorn_run.assert_called_once()
        kwargs = uvicorn_run.call_args.kwargs
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["reload"] is False
        assert "loopback=True" in result.output
        assert "Listening on 127.0.0.1" in result.output

    def test_explicit_loopback_ipv6_accepted(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        with _serve_context() as uvicorn_run:
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init", "--host", "::1"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert uvicorn_run.call_args.kwargs["host"] == "::1"

    def test_localhost_accepted(self, runner: CliRunner, workspace: Path) -> None:
        with _serve_context() as uvicorn_run:
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init", "--host", "localhost"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert uvicorn_run.call_args.kwargs["host"] == "localhost"

    def test_zero_dot_zero_dot_zero_dot_zero_refused(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        with _serve_context() as uvicorn_run:
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init", "--host", "0.0.0.0"],
            )
        assert result.exit_code != 0
        assert result.exit_code == 3
        uvicorn_run.assert_not_called()
        combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
        assert "Refusing non-loopback bind" in combined
        assert "0.0.0.0" in combined
        # Bind decision is logged before the refusal.
        assert "loopback=False" in result.output

    def test_ipv6_any_refused(self, runner: CliRunner, workspace: Path) -> None:
        with _serve_context() as uvicorn_run:
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init", "--host", "::"],
            )
        assert result.exit_code == 3
        uvicorn_run.assert_not_called()
        assert "Refusing non-loopback bind" in (result.output + getattr(result, "stderr", ""))

    def test_non_loopback_private_ip_refused(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        with _serve_context() as uvicorn_run:
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init", "--host", "192.168.1.10"],
            )
        assert result.exit_code == 3
        uvicorn_run.assert_not_called()
        combined = result.output + getattr(result, "stderr", "")
        assert "192.168.1.10" in combined
        assert "auth/authorization" in combined

    def test_reload_flag_still_propagated_when_explicit(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        with _serve_context() as uvicorn_run:
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init", "--reload"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert uvicorn_run.call_args.kwargs["reload"] is True

    def test_refusal_happens_before_workspace_side_effects(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        # A non-loopback bind must fail-closed BEFORE git init, env load or
        # DB table creation run, so no workspace state is mutated.
        with (
            patch("uvicorn.run") as uvicorn_run,
            patch("bridle.config.set_workspace") as set_ws,
            patch("bridle.features.workspace.git_initializer.GitWorkspaceInitializer") as InitCls,
            patch("bridle.cli._load_env_files", return_value=[]) as load_env,
            patch("bridle.database._ensure_engine") as ensure_engine,
        ):
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--host", "0.0.0.0"],
            )
        assert result.exit_code == 3
        uvicorn_run.assert_not_called()
        set_ws.assert_not_called()
        InitCls.assert_not_called()
        load_env.assert_not_called()
        ensure_engine.assert_not_called()
