"""Regression tests for serve() DB table bootstrap without coroutine leaks."""
from __future__ import annotations

import asyncio
import os
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from bridle.cli import _run_asyncio_blocking, app


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
def _serve_context(*, create_all: MagicMock | None = None):
    create_all_mock = create_all if create_all is not None else MagicMock()
    with (
        patch("bridle.features.workspace.git_initializer.GitWorkspaceInitializer", return_value=MagicMock()),
        patch("uvicorn.run"),
        patch("bridle.cli._load_env_files", return_value=[]),
        patch("bridle.config.set_workspace"),
        patch("bridle.models", create=True),
        patch("bridle.database._ensure_engine"),
        patch("bridle.database._engine") as eng_patch,
        patch("bridle.models.base.Base.metadata.create_all", create_all_mock),
    ):
        conn = MagicMock()

        async def _run_sync(fn: object, *args: object, **kwargs: object) -> None:
            if callable(fn):
                fn(*args, **kwargs)

        conn.run_sync = AsyncMock(side_effect=_run_sync)
        eng_patch.begin.return_value.__aenter__ = AsyncMock(return_value=conn)
        eng_patch.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        try:
            yield create_all_mock
        finally:
            os.environ.pop("BRIDLE_WORKSPACE", None)


class TestServeDbTables:
    def test_no_unawaited_coroutine_when_asyncio_run_is_noop(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        with (
            _serve_context(),
            patch("asyncio.run", side_effect=lambda coro: None) as run_mock,
            warnings.catch_warnings(record=True) as caught,
        ):
                warnings.simplefilter("always", RuntimeWarning)
                result = runner.invoke(
                    app,
                    [
                        "serve",
                        "--workspace",
                        str(workspace),
                        "--no-auto-git-init",
                    ],
                    catch_exceptions=False,
                )

        unawaited = [w for w in caught if "never awaited" in str(w.message)]
        assert unawaited == []
        assert result.exit_code == 0
        assert "DB tables ensured" in result.output
        run_mock.assert_called_once()
        coro_arg = run_mock.call_args.args[0]
        assert asyncio.iscoroutine(coro_arg)
        assert coro_arg.cr_frame is None

    def test_serve_passes_no_reload_flag_to_uvicorn(
        self,
        runner: CliRunner,
        workspace: Path,
    ) -> None:
        with _serve_context(), patch("uvicorn.run") as uvicorn_run:
            result = runner.invoke(
                app,
                    [
                        "serve",
                        "--workspace",
                        str(workspace),
                        "--no-auto-git-init",
                        "--no-reload",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        uvicorn_run.assert_called_once()
        assert uvicorn_run.call_args.kwargs["reload"] is False

    @pytest.mark.asyncio
    async def test_run_asyncio_blocking_with_running_event_loop(self) -> None:
        import bridle.database as db_mod
        from bridle.database import _ensure_engine
        from bridle.models.base import Base

        create_all = MagicMock()

        async def _create_tables() -> None:
            _ensure_engine()
            async with db_mod._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        conn = MagicMock()

        async def _run_sync(fn: object, *args: object, **kwargs: object) -> None:
            if callable(fn):
                fn(*args, **kwargs)

        conn.run_sync = AsyncMock(side_effect=_run_sync)

        with (
            patch("bridle.database._ensure_engine"),
            patch("bridle.database._engine") as eng_patch,
            patch("bridle.models.base.Base.metadata.create_all", create_all),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always", RuntimeWarning)
            eng_patch.begin.return_value.__aenter__ = AsyncMock(return_value=conn)
            eng_patch.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            _run_asyncio_blocking(_create_tables)

        unawaited = [w for w in caught if "never awaited" in str(w.message)]
        assert unawaited == []
        create_all.assert_called_once()

