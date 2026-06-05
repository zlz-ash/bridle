"""Shared test fixtures — workspace-anchored, no C-drive temp leakage.

All test paths are derived from backend/.test-workspaces/<test-name>/.
Default tests use SQLite :memory: to avoid file I/O issues.
Only restart-recovery tests use file-based SQLite under the test workspace.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
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


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.wintypes.LPVOID),
        ("bInheritHandle", ctypes.wintypes.BOOL),
    ]


def _mkdir_test_workspace(path: Path) -> None:
    """Create a test workspace whose files can be deleted on Windows."""
    if os.name != "nt":
        path.mkdir(parents=True, exist_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    security_descriptor = ctypes.wintypes.LPVOID()
    descriptor_size = ctypes.wintypes.ULONG()

    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.LPVOID),
        ctypes.POINTER(ctypes.wintypes.ULONG),
    ]
    convert.restype = ctypes.wintypes.BOOL

    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    ]
    create_directory.restype = ctypes.wintypes.BOOL

    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.wintypes.HLOCAL]
    local_free.restype = ctypes.wintypes.HLOCAL

    # Test workspaces live under a sandboxed project tree that can lack delete
    # rights. Give this per-test directory an inheritable DACL so remove-patch
    # tests exercise real deletion instead of being blocked by host ACLs.
    sddl = "D:P(A;OICI;FA;;;WD)"
    if not convert(sddl, 1, ctypes.byref(security_descriptor), ctypes.byref(descriptor_size)):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        security_attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            security_descriptor,
            False,
        )
        if not create_directory(str(path), ctypes.byref(security_attributes)):
            error = ctypes.get_last_error()
            if not path.exists():
                raise ctypes.WinError(error)
    finally:
        if security_descriptor:
            local_free(security_descriptor)


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
    safe_name = test_name
    for char in '<>:"|?*':
        safe_name = safe_name.replace(char, "_")
    safe_name = (
        safe_name.replace("[", "_")
        .replace("]", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )
    ws = TEST_WORKSPACES_ROOT / f"{safe_name[:80]}-{uuid4().hex[:8]}"
    _mkdir_test_workspace(ws)

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

    The database file lives under <test_workspace>/.aicoding/.
    Only use this for tests that need data to persist across sessions.
    """
    from bridle.config import get_config

    config = get_config()
    config.aicoding_dir.mkdir(parents=True, exist_ok=True)
    return config.aicoding_dir / f"recovery-{uuid4().hex}.sqlite3"


@pytest.fixture(autouse=True)
def _stub_complexity_negotiation_llm(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Deterministic negotiation in tests — never call a real LLM."""
    nodeid = request.node.nodeid
    if "test_complexity_negotiation_service" in nodeid:
        return
    if "test_plan_import_negotiation.py::TestRunImportComplexityNegotiation" in nodeid:
        return
    from bridle.schemas.complexity_negotiation import NegotiationDecision, validate_negotiation_decision
    from bridle.services import complexity_negotiation_service as cns

    async def _fake_negotiate(
        self: cns.ComplexityNegotiationService,
        *,
        plan_nodes: list,
        validation_issues: list,
        round_index: int,
        max_rounds: int = cns._IMPORT_MAX_ROUNDS,
    ) -> NegotiationDecision:
        failing = [v for v in validation_issues if not v.ok]
        target_id = failing[0].node_id if failing else (plan_nodes[0].id if plan_nodes else "n1")
        node = next((n for n in plan_nodes if n.id == target_id), plan_nodes[0])
        node.estimated_minutes = max(node.estimated_minutes or 0, 60)
        if len((node.goal or "").strip()) < 40:
            node.goal = (node.goal or "Work") + " with clear acceptance criteria for reviewers"
        if getattr(node, "acceptance_scope", None) in (None, ""):
            node.acceptance_scope = "Deliverable meets integration acceptance criteria"
        if len(node.files) > 5:
            node.files = node.files[:5]
        return validate_negotiation_decision(
            {
                "action": "expand",
                "expand": {
                    "node_id": target_id,
                    "new_goal": node.goal,
                    "new_acceptance_scope": node.acceptance_scope or node.goal,
                    "new_estimated_minutes": node.estimated_minutes,
                    "additional_files": [],
                },
            }
        )

    monkeypatch.setattr(cns.ComplexityNegotiationService, "negotiate", _fake_negotiate)

    def _fake_default() -> cns.ComplexityNegotiationService:
        return cns.ComplexityNegotiationService(object())

    monkeypatch.setattr(cns.ComplexityNegotiationService, "default", staticmethod(_fake_default))


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
        except asyncio.TimeoutError:
            serve_task.cancel()
