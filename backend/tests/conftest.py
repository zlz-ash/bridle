"""Shared test fixtures — workspace-anchored, no C-drive temp leakage.

All test paths are derived from backend/.test-workspaces/<test-name>/.
Default tests use SQLite :memory: to avoid file I/O issues.
Only restart-recovery tests use file-based SQLite under the test workspace.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bridle.config import set_workspace
import bridle.models  # noqa: F401 — register all ORM tables
from bridle.models.base import Base

TEST_WORKSPACES_ROOT = Path(__file__).resolve().parent / ".test-workspaces"

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

    Located under backend/.test-workspaces/<sanitized-test-name>/.
    Calls set_workspace() so that get_config() returns paths anchored here.
    Sets up a minimal git repo so that coding session creation succeeds by default.
    """
    test_name = request.node.name
    safe_name = (
        test_name.replace("[", "_")
        .replace("]", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )
    ws = TEST_WORKSPACES_ROOT / f"{safe_name[:80]}-{uuid4().hex[:8]}"
    ws.mkdir(parents=True, exist_ok=True)

    needs_custom_git = (
        request.node.path.name == "test_git_workspace_policy.py"
        or test_name in {
            "test_git_preflight_failure_marks_session_failed",
            "test_refuses_non_git_workspace",
        }
    )
    if not needs_custom_git:
        git_dir = ws / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

    set_workspace(ws)

    logger.debug("Test workspace created: %s", ws)
    return ws


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
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

    logger.debug("In-memory database torn down for workspace: %s", test_workspace)


@pytest.fixture
def recovery_db_path(test_workspace: Path) -> Path:
    """Provide a file-based SQLite path for restart recovery tests.

    The database file lives under <test_workspace>/.aicoding/.
    Only use this for tests that need data to persist across sessions.
    """
    from bridle.config import get_config

    config = get_config()
    config.aicoding_dir.mkdir(parents=True, exist_ok=True)
    return config.aicoding_dir / f"recovery-{uuid4().hex}.sqlite3"


@pytest_asyncio.fixture
async def client(
    db: AsyncSession, test_workspace: Path
) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient wired to the FastAPI test app."""
    from bridle.app import create_app

    app = create_app(test_db=db, test_workspace=str(test_workspace))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
