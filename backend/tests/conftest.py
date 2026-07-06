"""Shared test fixtures — workspace-anchored, no C-drive temp leakage.

All test paths are derived from backend/.test-workspaces/<test-name>/.
Default tests use SQLite :memory: to avoid file I/O issues.
Only restart-recovery tests use file-based SQLite under the test workspace.

Workspace creation, identity registration and ACL-baseline teardown are
delegated to ``_workspace_lifecycle`` so the container conftest shares the
same zero-leftover contract.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import bridle.models  # noqa: F401 — register all ORM tables
from bridle.config import set_workspace
from bridle.models.base import Base
from tests._workspace_lifecycle import (
    create_workspace,
    teardown_workspace,
)

TEST_WORKSPACES_ROOT = Path(__file__).resolve().parent.parent / ".test-workspaces"

logger = logging.getLogger("bridle.test")


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _reset_global_workspace() -> None:
    """Reset global workspace state before every test function."""
    import bridle.config as _cfg

    _cfg._global_config = None


@pytest.fixture
def test_workspace(request) -> Path:
    """Provide a unique workspace directory for each test function.

    Located under backend/.test-workspaces/<sanitized-test-name>/. Calls
    set_workspace() so that get_config() returns paths anchored here.
    Sets up a minimal git repo for tests that exercise git-aware workspace
    code. Teardown verifies identity, restores the ACL baseline and deletes
    the directory; cleanup failure fails the test with a diagnostic while
    preserving the main test result.
    """
    test_name = request.node.name
    needs_custom_git = (
        request.node.path.name == "test_git_workspace_policy.py"
        or test_name in {
            "test_git_preflight_failure_marks_session_failed",
            "test_refuses_non_git_workspace",
        }
    )
    ws, identity = create_workspace(
        test_name,
        TEST_WORKSPACES_ROOT,
        with_git=not needs_custom_git,
    )
    set_workspace(ws)
    logger.debug("Test workspace created: %s", ws)
    yield ws
    cleanup_error = teardown_workspace(ws, identity)
    if cleanup_error:
        raise AssertionError(
            f"workspace cleanup failed for {ws}: {cleanup_error}"
        )


@pytest.fixture
def tmp_path(test_workspace: Path) -> Path:
    """Provide a pytest tmp_path equivalent under the D-drive test workspace."""
    path = test_workspace / ".pytest-tmp" / "tmp_path"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest_asyncio.fixture
async def db(test_workspace: Path) -> AsyncGenerator[AsyncSession, None]:
    """Provide a fresh in-memory SQLite session for each test.

    Uses SQLite :memory: to avoid file I/O issues.
    File outputs (JSON mirror, runs, logs, reports) still go to the test workspace.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        from bridle.api.deps import set_test_db

        set_test_db(session)
        yield session

    from bridle.api.deps import clear_test_db

    clear_test_db()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

    logger.debug("In-memory database torn down for workspace: %s", test_workspace)


@pytest.fixture
def recovery_db_path(test_workspace: Path) -> Path:
    """Provide a file-based SQLite path for restart recovery tests.

    The database file lives under the workspace runtime directory.
    Only use this for tests that need data to persist across sessions.
    """
    from bridle.config import get_config

    config = get_config()
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    return config.runtime_dir / f"recovery-{uuid4().hex}.sqlite3"


@pytest.fixture(autouse=True)
def _stub_complexity_negotiation_llm(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """The retired plan-import chain no longer needs a global LLM stub."""
    return


@pytest_asyncio.fixture
async def client(
    db: AsyncSession, test_workspace: Path
) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient wired to the FastAPI test app."""
    from bridle.agent.container.container_service import reset_for_tests
    from bridle.agent.container.runner import FakeContainerRunner
    from bridle.app import create_app

    reset_for_tests()
    app = create_app(
        test_db=db,
        test_workspace=str(test_workspace),
        container_runner=FakeContainerRunner(workspace_root=test_workspace),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _pick_live_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest_asyncio.fixture
async def live_client(
    db: AsyncSession, test_workspace: Path
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client backed by a real uvicorn server (required for SSE tests)."""
    import asyncio

    import uvicorn

    from bridle.app import create_app

    app = create_app(test_db=db, test_workspace=str(test_workspace))
    port = _pick_live_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.01)
    if not server.started:
        server.should_exit = True
        serve_task.cancel()
        raise RuntimeError("uvicorn test server failed to start")

    client = AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
    try:
        yield client
    finally:
        await client.aclose()
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except TimeoutError:
            serve_task.cancel()
