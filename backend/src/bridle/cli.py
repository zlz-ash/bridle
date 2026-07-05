"""Bridle CLI entry point."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv


def _load_env_files(workspace: Path) -> list[Path]:
    """Load .env files. Priority: workspace/.env > backend/.env (does not override real env)."""
    loaded: list[Path] = []
    # backend/.env sits at repo root: cli.py -> bridle -> src -> backend
    backend_env = Path(__file__).resolve().parents[2] / ".env"
    for candidate in (workspace / ".env", backend_env):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            loaded.append(candidate)
    return loaded

app = typer.Typer(name="bridle", help="Project-map runtime for Bridle workspaces")

obs_app = typer.Typer(name="obs", help="Observability diagnostics")
app.add_typer(obs_app, name="obs")

_LANGFUSE_SDK_METHODS = (
    "start_observation",
    "flush",
)


@app.command()
def version() -> None:
    """Show version."""
    from bridle import __version__

    typer.echo(f"bridle {__version__}")


def _run_asyncio_blocking(coro_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run a one-shot coroutine from sync CLI code without leaking coroutines."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        coro = coro_factory()
        try:
            asyncio.run(coro)
        finally:
            if asyncio.iscoroutine(coro) and coro.cr_frame is not None:
                coro.close()
        return

    def _in_thread() -> None:
        asyncio.run(coro_factory())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_in_thread).result()


@app.command()
def serve(
    workspace: Path = typer.Option(
        ..., "--workspace", "-w",
        help="Path to the workspace directory (required)",
        exists=True, file_okay=False, resolve_path=True,
    ),
    host: str = "0.0.0.0",
    port: int = 8900,
    no_auto_git_init: bool = typer.Option(
        False,
        "--no-auto-git-init",
        help="涓嶈嚜鍔ㄦ妸 workspace 鍒濆鍖栦负 git 浠撳簱锛堥珮绾х敤鎴凤級",
    ),
    reload: bool = typer.Option(
        True,
        "--reload/--no-reload",
        help="Enable uvicorn reload watcher for dev runs.",
    ),
) -> None:
    """Start the API server anchored to a workspace."""
    from bridle.config import set_workspace

    set_workspace(workspace)
    # uvicorn reload mode forks a worker that does NOT inherit our module globals;
    # propagate workspace via env so the child can recover via get_config() fallback.
    os.environ["BRIDLE_WORKSPACE"] = str(workspace)
    typer.echo(f"Workspace: {workspace}")

    if not no_auto_git_init:
        from bridle.features.workspace.git_initializer import (
            GitWorkspaceInitError,
            GitWorkspaceInitializer,
        )

        try:
            GitWorkspaceInitializer(workspace, log=typer.echo).ensure_repo()
        except GitWorkspaceInitError as exc:
            typer.echo(f"鍚姩澶辫触 [{exc.code}]: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    loaded = _load_env_files(workspace)
    for env_path in loaded:
        typer.echo(f"Loaded env: {env_path}")

    # Ensure tables exist in the workspace SQLite DB (no alembic in repo yet).
    import bridle.models  # noqa: F401 -register all ORM tables
    from bridle.database import _ensure_engine
    from bridle.models.base import Base
    import bridle.database as _db_mod

    async def _create_tables() -> None:
        _ensure_engine()
        async with _db_mod._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run_asyncio_blocking(_create_tables)
    typer.echo("DB tables ensured")

    if os.getenv("BRIDLE_AGENT_PROVIDER", "fake") != "fake":
        typer.echo(f"Provider: {os.getenv('BRIDLE_AGENT_PROVIDER')}  Model: {os.getenv('BRIDLE_AGENT_MODEL', '?')}")

    import uvicorn

    uvicorn.run("bridle.app:create_app", host=host, port=port, reload=reload, factory=True)


@obs_app.command("check")
def obs_check() -> None:
    """Verify Langfuse observability configuration and emit a test trace."""
    from bridle.observability.config import ObservabilityConfig
    from bridle.observability.facade import ObservabilityFacade
    from bridle.observability.noop_adapter import NoopObservabilityAdapter

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

    try:
        import langfuse

        sdk_version = langfuse.__version__
    except Exception:
        typer.echo("langfuse SDK not installed", err=True)
        raise typer.Exit(code=1) from None

    config = ObservabilityConfig.from_env()
    typer.echo(f"provider={config.provider}")
    typer.echo(f"host={config.langfuse_host or '(unset)'}")
    typer.echo(f"langfuse_sdk={sdk_version}")

    facade = ObservabilityFacade(config)
    if isinstance(facade.adapter, NoopObservabilityAdapter):
        typer.echo("Observability is noop -check LANGFUSE_* credentials or SDK compatibility", err=True)
        raise typer.Exit(code=2)

    client = facade.adapter._client

    for method_name in _LANGFUSE_SDK_METHODS:
        present = callable(getattr(client, method_name, None))
        typer.echo(f"method {method_name}: {'ok' if present else 'MISSING'}")
        if not present:
            typer.echo(f"langfuse SDK incompatible: missing {method_name}", err=True)
            raise typer.Exit(code=3)

    trace = facade.start_trace("cli.check", phase="obs_check")
    facade.record_generation(
        model="cli-check",
        input_summary={"source": "bridle obs check"},
        output_summary={"status": "ok"},
    )
    trace.end(status="completed")
    facade.flush()

    trace_id = str(getattr(trace, "trace_id", "") or "")
    typer.echo(f"trace_id={trace_id}")

    if callable(getattr(client, "get_trace_url", None)):
        try:
            trace_url = client.get_trace_url(trace_id=trace_id) if trace_id else client.get_trace_url()
        except TypeError:
            trace_url = client.get_trace_url()
        except Exception:
            trace_url = None
        else:
            if trace_url:
                typer.echo(f"trace_url={trace_url}")


if __name__ == "__main__":
    app()


