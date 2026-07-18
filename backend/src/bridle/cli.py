"""Bridle CLI entry point."""
from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import os
import socket
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback_host(host: str) -> tuple[bool, str]:
    """Classify a bind host. Returns ``(is_loopback, reason)``.

    The API has no auth/authorization contract, so non-loopback binds are
    fail-closed. ``0.0.0.0`` / ``::`` and any host that resolves to a
    non-loopback address are rejected.
    """
    h = host.strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in _LOOPBACK_HOSTS:
        return True, "loopback literal"
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback, f"ip={ip} is_loopback={ip.is_loopback}"
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror as exc:
        return False, f"unresolvable host {host!r}: {exc}"
    for _fam, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, f"non-IP resolved address {addr!r}"
        if not ip.is_loopback:
            return False, f"resolves to non-loopback {ip}"
    return True, "resolved to loopback only"


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
code_app = typer.Typer(name="code", help="Deterministic candidate code observation")
app.add_typer(code_app, name="code")

_LANGFUSE_SDK_METHODS = (
    "start_observation",
    "flush",
)


@app.command()
def version() -> None:
    """Show version."""
    from bridle import __version__

    typer.echo(f"bridle {__version__}")


def get_control_service(workspace: Path):
    """Return the shared application-service facade for one workspace."""
    from bridle.features.project_map.service import ControlService

    return ControlService(workspace)


def _read_control_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("control_payload_must_be_object")
    return parsed


def _invoke_control(operation: str, workspace: Path, *, json_output: bool) -> None:
    try:
        payload = _read_control_payload()
        result = get_control_service(workspace).invoke(operation, payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        result = {
            "status": "failed",
            "operation": operation,
            "error_code": "invalid_control_input",
            "message": str(exc),
            "changed_ids": [],
            "artifact_ref": None,
        }
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, default=str))
    else:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get("status") != "completed":
        raise typer.Exit(code=2)


@code_app.command("inspect")
def code_inspect(
    workspace: Path = typer.Option(..., "--workspace", "-w", exists=True, file_okay=False),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Inspect bounded candidate code metadata."""
    _invoke_control("code.inspect", workspace, json_output=json_output)


@code_app.command("search")
def code_search(
    workspace: Path = typer.Option(..., "--workspace", "-w", exists=True, file_okay=False),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Search bounded candidate code metadata."""
    _invoke_control("code.search", workspace, json_output=json_output)


@code_app.command("graph")
def code_graph(
    workspace: Path = typer.Option(..., "--workspace", "-w", exists=True, file_okay=False),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Read a bounded code graph slice."""
    _invoke_control("code.graph", workspace, json_output=json_output)


def _control_command(operation: str, workspace: Path, json_output: bool) -> None:
    _invoke_control(operation, workspace, json_output=json_output)


@app.command("plan")
def plan_command(
    workspace: Path = typer.Option(..., "--workspace", "-w", exists=True, file_okay=False),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Read or query the project plan through the shared control service."""
    _control_command("plan", workspace, json_output)


@app.command("agent")
def agent_command(
    workspace: Path = typer.Option(..., "--workspace", "-w", exists=True, file_okay=False),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Read agent workflow events through the shared control service."""
    _control_command("agent", workspace, json_output)


@app.command("candidate")
def candidate_command(
    workspace: Path = typer.Option(..., "--workspace", "-w", exists=True, file_okay=False),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Read candidate submission summaries through the shared control service."""
    _control_command("candidate", workspace, json_output)


@app.command("verify")
def verify_command(
    workspace: Path = typer.Option(..., "--workspace", "-w", exists=True, file_okay=False),  # noqa: B008
    json_output: bool = typer.Option(False, "--json"),  # noqa: B008
) -> None:
    """Read verification status through the shared control service."""
    _control_command("verify", workspace, json_output)


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
    workspace: Path = typer.Option(  # noqa: B008
        ..., "--workspace", "-w",
        help="Path to the workspace directory (required)",
        exists=True, file_okay=False, resolve_path=True,
    ),
    host: str = typer.Option(  # noqa: B008
        "127.0.0.1",
        "--host",
        help="Bind host. Defaults to loopback; non-loopback is rejected without a complete auth contract.",
    ),
    port: int = typer.Option(8900, "--port", help="Bind port."),  # noqa: B008
    no_auto_git_init: bool = typer.Option(
        False,
        "--no-auto-git-init",
        help="不自动把 workspace 初始化为 git 仓库（高级用户）",
    ),
    reload: bool = typer.Option(  # noqa: B008
        False,
        "--reload/--no-reload",
        help="Enable uvicorn reload watcher. Defaults to False; reload forks a worker with no module globals.",
    ),
) -> None:
    """Start the API server anchored to a workspace."""
    from bridle.config import set_workspace

    is_loopback, reason = _is_loopback_host(host)
    typer.echo(f"Bind decision: host={host!r} loopback={is_loopback} reason={reason}")
    if not is_loopback:
        typer.echo(
            f"Refusing non-loopback bind {host!r}: {reason}. "
            "Bridle API has no auth/authorization/CORS/transport contract; "
            "exposing it would let any network peer read the workspace and "
            "trigger state changes. Bind to 127.0.0.1 / ::1 / localhost, or "
            "front the API with a reverse proxy that enforces auth.",
            err=True,
        )
        raise typer.Exit(code=3)

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
            typer.echo(f"启动失败 [{exc.code}]: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    loaded = _load_env_files(workspace)
    for env_path in loaded:
        typer.echo(f"Loaded env: {env_path}")

    # Ensure tables exist in the workspace SQLite DB (no alembic in repo yet).
    import bridle.database as _db_mod
    import bridle.models  # noqa: F401 -register all ORM tables
    from bridle.database import _ensure_engine
    from bridle.models.base import Base

    async def _create_tables() -> None:
        _ensure_engine()
        async with _db_mod._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    _run_asyncio_blocking(_create_tables)
    typer.echo("DB tables ensured")

    if os.getenv("BRIDLE_AGENT_PROVIDER", "fake") != "fake":
        typer.echo(f"Provider: {os.getenv('BRIDLE_AGENT_PROVIDER')}  Model: {os.getenv('BRIDLE_AGENT_MODEL', '?')}")

    typer.echo(f"Listening on {host}:{port} (loopback only, reload={reload})")
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


