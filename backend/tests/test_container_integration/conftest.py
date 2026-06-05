"""Fixtures for real Docker integration tests."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.engine.container_runner import ContainerRunner, LocalContainerRuntimeRunner


def _image_ready(name: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _api_key_ready() -> bool:
    return bool(os.environ.get("BRIDLE_AGENT_API_KEY", "").strip())


@pytest.fixture
def require_docker_images() -> None:
    if not _image_ready("bridle-node-agent:local") or not _image_ready("bridle-main-agent:local"):
        pytest.skip("run scripts/build-images.ps1 first")


@pytest.fixture
def require_agent_api_key() -> None:
    if not _api_key_ready():
        pytest.skip("BRIDLE_AGENT_API_KEY required for real LLM container tests")


@pytest.fixture
def docker_container_runner(test_workspace: Path) -> LocalContainerRuntimeRunner:
    runner = LocalContainerRuntimeRunner(test_workspace, use_docker=True)
    if not runner.use_docker:
        pytest.skip("docker executable not available")
    return runner


@pytest.fixture
def force_docker_container_runner(
    test_workspace: Path,
    docker_container_runner: LocalContainerRuntimeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> LocalContainerRuntimeRunner:
    def _resolve(
        workspace_root: str | Path,
        *,
        runner: ContainerRunner | None = None,
    ) -> ContainerRunner:
        if runner is not None:
            return runner
        return docker_container_runner

    monkeypatch.setattr(
        "bridle.engine.container_runner_factory.resolve_container_runner",
        _resolve,
    )
    return docker_container_runner


@pytest_asyncio.fixture
async def docker_backend_on_8900(
    db: AsyncSession,
    test_workspace: Path,
    force_docker_container_runner: LocalContainerRuntimeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[AsyncClient, object], None]:
    """Bind backend on 0.0.0.0:8900 so containers reach it via host.docker.internal."""
    import asyncio
    import socket

    import uvicorn
    from bridle.app import create_app

    port = 8900
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            pytest.skip(f"port {port} already in use; stop other backend first")

    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    monkeypatch.delenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", raising=False)
    monkeypatch.setattr(
        "bridle.services.node_agent_worker.ContainerOutputSimulator.should_simulate",
        lambda _workspace: False,
    )

    app = create_app(test_db=db, test_workspace=str(test_workspace))
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="error",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.02)
    if not server.started:
        server.should_exit = True
        serve_task.cancel()
        raise RuntimeError("uvicorn failed to start on :8900")

    client = AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30.0)
    try:
        yield client, server
    finally:
        await client.aclose()
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=10.0)
        except asyncio.TimeoutError:
            serve_task.cancel()
