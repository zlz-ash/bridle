"""Health API tests."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok(db, test_workspace) -> None:
    from bridle.app import create_app

    app = create_app(test_db=db, test_workspace=str(test_workspace))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.perf_counter()
        response = await client.get("/api/v1/health")
        elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert "version" in body
    assert str(test_workspace.resolve()) in body["workspace"]
    assert body["uptime_seconds"] >= 0
    assert "events_subscribers" in body
    assert elapsed_ms < 200
    blob = json.dumps(body)
    for secret in ("api_key", "token", "dsn", "password"):
        assert secret not in blob.lower()


@pytest.mark.asyncio
async def test_health_degraded_when_db_fails(test_workspace) -> None:
    from bridle.app import create_app
    from bridle.api import deps

    app = create_app(test_workspace=str(test_workspace))

    class BrokenSession:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("db down")

    async def broken_get_db():
        yield BrokenSession()

    app.dependency_overrides[deps.get_db] = broken_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"].startswith("error:")


@pytest.mark.asyncio
async def test_health_reports_sse_subscribers(live_client: AsyncClient) -> None:
    async with live_client.stream("GET", "/api/v1/events"):
        async with live_client.stream("GET", "/api/v1/events"):
            await asyncio.sleep(0.2)
            health = await live_client.get("/api/v1/health")
            assert health.status_code == 200
            assert health.json()["events_subscribers"] >= 2
