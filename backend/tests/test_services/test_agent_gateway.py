"""AgentGateway orchestration tests — timeouts, persistence, boundary, RPC codes."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from bridle.models.proposal import ProposalRecord
from bridle.schemas.proposal import AgentProposalSchema, FilePatchSchema
from bridle.services.agent_gateway import (
    AgentGateway,
    _AgentProviderError,
    _ProposalBoundaryError,
)


async def _import_minimal_plan(client: AsyncClient, tests: bool = True) -> tuple[str, str]:
    """Return (task_id, node_uuid)."""
    task_resp = await client.post("/api/v1/tasks", json={"title": "Gateway Test"})
    task_id = task_resp.json()["id"]
    plan = {
        "goal": "Gateway",
        "nodes": [
            {
                "id": "n1",
                "title": "N1",
                "goal": "G1",
                "node_type": "code_change",
                "files": ["src/main.py"],
                "tests": ["pytest tests/"] if tests else [],
                "metrics": {},
                "constraints": {"c": True} if tests else [],
                "review_checks": [],
                "expected_outputs": {},
            },
        ],
    }
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
    assert imp.status_code == 200
    node_id = imp.json()["nodes"][0]["id"]
    return task_id, node_id


class SlowProvider:
    name = "slow_mock"

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def generate(self, ctx):
        await asyncio.sleep(self._delay)
        return AgentProposalSchema(
            summary="Should not persist",
            file_patches=[],
            tests_to_run=[],
        )


class ExplodingProvider:
    name = "boom"

    async def generate(self, ctx):
        raise RuntimeError("simulated upstream failure")


class TestAgentGatewayService:
    """Direct AgentGateway integration with patched providers."""

    @pytest.mark.asyncio
    async def test_timeout_does_not_persist(self, client: AsyncClient, db, monkeypatch) -> None:
        monkeypatch.setenv("BRIDLE_AGENT_TIMEOUT_SECONDS", "1")
        _, node_id = await _import_minimal_plan(client)

        with patch(
            "bridle.services.agent_gateway.AgentProviderFactory.create",
            return_value=SlowProvider(3.0),
        ):
            with pytest.raises(_AgentProviderError) as ei:
                await AgentGateway.create_proposal(db, node_id, "timeout case")
            assert ei.value.reason == "timeout"

        res = await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))
        assert res.scalars().all() == []

    @pytest.mark.asyncio
    async def test_provider_exception_no_persist(self, client: AsyncClient, db, monkeypatch) -> None:
        monkeypatch.setenv("BRIDLE_AGENT_TIMEOUT_SECONDS", "30")
        _, node_id = await _import_minimal_plan(client)

        with patch(
            "bridle.services.agent_gateway.AgentProviderFactory.create",
            return_value=ExplodingProvider(),
        ):
            with pytest.raises(_AgentProviderError) as ei:
                await AgentGateway.create_proposal(db, node_id, "boom")
            assert ei.value.reason == "RuntimeError"

        res = await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))
        assert res.scalars().all() == []

    @pytest.mark.asyncio
    async def test_empty_summary_via_model_construct_no_persist(self, client: AsyncClient, db, monkeypatch) -> None:
        monkeypatch.setenv("BRIDLE_AGENT_TIMEOUT_SECONDS", "30")
        _, node_id = await _import_minimal_plan(client)

        bogus = AgentProposalSchema.model_construct(
            summary="   ",
            file_patches=[],
            tests_to_run=[],
        )

        class WeirdProvider:
            name = "weird"

            async def generate(self, ctx):
                return bogus

        with patch(
            "bridle.services.agent_gateway.AgentProviderFactory.create",
            return_value=WeirdProvider(),
        ):
            with pytest.raises(_AgentProviderError) as ei:
                await AgentGateway.create_proposal(db, node_id, "bad summary")
            assert ei.value.reason == "EmptySummary"

        res = await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))
        assert res.scalars().all() == []

    @pytest.mark.asyncio
    async def test_path_boundary_raises_with_path_hint(self, client: AsyncClient, db, monkeypatch) -> None:
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "configured_stub")
        monkeypatch.setenv("BRIDLE_AGENT_API_KEY", "sk-secret")
        monkeypatch.setenv("BRIDLE_AGENT_TIMEOUT_SECONDS", "30")
        _, node_id = await _import_minimal_plan(client)

        fp = FilePatchSchema.model_construct(path="../secret.py", change_type="modify", diff="x")
        proposal_bad = AgentProposalSchema.model_construct(
            summary="Has bad path",
            file_patches=[fp],
            tests_to_run=[],
        )

        class BoundaryProvider:
            name = "boundary_mock"

            async def generate(self, ctx):
                return proposal_bad

        with patch(
            "bridle.services.agent_gateway.AgentProviderFactory.create",
            return_value=BoundaryProvider(),
        ):
            with pytest.raises(_ProposalBoundaryError) as ei:
                await AgentGateway.create_proposal(db, node_id, "cross boundary")

        detail = ei.value.details
        assert "errors" in detail
        assert detail.get("path") == "../secret.py"

        res = await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))
        assert res.scalars().all() == []


class TestAgentProposalHttpErrorCodes:
    """Public API exposes plan-specific conflict codes."""

    @pytest_asyncio.fixture
    async def node_for_proposals(self, client: AsyncClient) -> str:
        _, node_id = await _import_minimal_plan(client)
        return node_id

    async def test_agent_provider_error_code_and_shape(self, client: AsyncClient, node_for_proposals, monkeypatch):
        monkeypatch.setenv("BRIDLE_AGENT_TIMEOUT_SECONDS", "1")
        with patch(
            "bridle.services.agent_gateway.AgentProviderFactory.create",
            return_value=SlowProvider(3.0),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{node_for_proposals}/agent/proposals",
                json={"instruction": "Slow"},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "agent_provider_error"
        assert body["resource"] == "proposal"
        assert body["details"]["reason"] == "timeout"
        assert "provider" in body["details"]

    async def test_proposal_boundary_error_code(self, client: AsyncClient, node_for_proposals, monkeypatch):
        monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "configured_stub")
        monkeypatch.setenv("BRIDLE_AGENT_API_KEY", "k")
        monkeypatch.setenv("BRIDLE_AGENT_TIMEOUT_SECONDS", "30")

        bad_prop = AgentProposalSchema(
            summary="Summary ok",
            file_patches=[
                FilePatchSchema(path="notlisted.py", change_type="modify", diff="."),
            ],
            tests_to_run=[],
        )

        class P:
            name = "p"

            async def generate(self, ctx):
                return bad_prop

        with patch("bridle.services.agent_gateway.AgentProviderFactory.create", return_value=P()):
            resp = await client.post(
                f"/api/v1/nodes/{node_for_proposals}/agent/proposals",
                json={"instruction": "X"},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == "proposal_boundary_error"
        assert body["details"]["path"] == "notlisted.py"
        assert "errors" in body["details"]

    async def test_blocked_node_does_not_invoke_provider(self, client: AsyncClient, monkeypatch):
        from bridle.engine.blocker import BlockResult

        task_resp = await client.post("/api/v1/tasks", json={"title": "Blocked provider"})
        task_id = task_resp.json()["id"]
        plan = {
            "goal": "B",
            "nodes": [
                {
                    "id": "n1",
                    "title": "N1",
                    "goal": "G1 with clear acceptance criteria for reviewers",
                    "node_type": "code_change",
                    "files": ["src/main.py"],
                    "tests": ["pytest tests/test_main.py -q"],
                    "constraints": {"c": True},
                    "metrics": {},
                    "review_checks": [],
                    "expected_outputs": {},
                },
            ],
        }
        imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        node_id = imp.json()["nodes"][0]["id"]

        class MustNotRun:
            name = "forbidden"

            async def generate(self, ctx):
                raise AssertionError("provider must not run for blocked nodes")

        with patch(
            "bridle.services.agent_gateway.Blocker.check",
            return_value=BlockResult(blocked=True, reason="Missing test definitions"),
        ), patch("bridle.services.agent_gateway.AgentProviderFactory.create", return_value=MustNotRun()):
            resp = await client.post(
                f"/api/v1/nodes/{node_id}/agent/proposals",
                json={"instruction": "Try"},
            )

        assert resp.status_code == 409
        assert resp.json().get("code") == "conflict"
