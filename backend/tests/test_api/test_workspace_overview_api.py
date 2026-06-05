"""Workspace overview API tests."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_workspace_overview_empty(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspace/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["is_empty"] is True
    assert body["file_count"] == 0
    assert body["files"] == []
    assert body["excerpts"] == {}


@pytest.mark.asyncio
async def test_workspace_overview_lists_files(client: AsyncClient, test_workspace) -> None:
    (test_workspace / "README.md").write_text("hi", encoding="utf-8")
    (test_workspace / "main.py").write_text("print(1)", encoding="utf-8")
    response = await client.get("/api/v1/workspace/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["is_empty"] is False
    assert body["file_count"] >= 2
    assert "README.md" in body["files"]
    assert body["excerpts"]["README.md"] == "hi"
