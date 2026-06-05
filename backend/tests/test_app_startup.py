"""FastAPI app startup hooks."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from bridle.app import create_app


@pytest.mark.asyncio
async def test_startup_reconciles_active_sessions(test_workspace, monkeypatch) -> None:
    monkeypatch.delenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", raising=False)
    mock_db = AsyncMock()

    @asynccontextmanager
    async def fake_session():
        yield mock_db

    with patch("bridle.database.async_session", fake_session), patch(
        "bridle.services.session_reconciler.SessionReconciler.reconcile_on_startup",
        new_callable=AsyncMock,
        return_value={"already_running": [], "restarted": [], "failed": []},
    ) as reconcile:
        app = create_app(test_workspace=str(test_workspace))
        async with app.router.lifespan_context(app):
            pass
        reconcile.assert_awaited_once()
