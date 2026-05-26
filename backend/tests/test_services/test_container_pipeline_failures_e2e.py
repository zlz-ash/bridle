"""Failure-path E2E for containerized node pipeline."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.container_output_simulator import ContainerOutputSimulator
from bridle.services.node_agent_worker import NodeAgentWorkerService
from bridle.services.node_container_orchestrator import NodeContainerError, NodeContainerOrchestrator
from tests.helpers.container_e2e import (
    assert_lock_released,
    start_containerized_run,
)
from tests.helpers.plan_factory import code_change_node


@pytest.mark.asyncio
async def test_e2e_missing_manifest_fails_releases_lock_and_diagnostic(
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await start_containerized_run(
        db=db, client=client, test_workspace=test_workspace, monkeypatch=monkeypatch,
        disable_simulator=True,
    )
    await NodeAgentWorkerService.run_once(ctx["run_id"], db=db)

    run = (await db.execute(
        select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == ctx["run_id"])
    )).scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "container_output_missing"
    await assert_lock_released(db, ctx["node_id"])

    runs_resp = await client.get(f"/api/v1/nodes/{ctx['node_id']}/runs")
    latest = runs_resp.json()[0]
    assert latest.get("error_code") == "container_output_missing"


@pytest.mark.asyncio
async def test_e2e_baseline_mismatch_does_not_apply_write(
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await start_containerized_run(
        db=db, client=client, test_workspace=test_workspace, monkeypatch=monkeypatch,
    )
    target = test_workspace / "src" / "e2e.py"
    original_write_for_run = ContainerOutputSimulator.write_for_run

    def write_mismatched_baseline(self, **kwargs):
        kwargs["baseline_revision"] = "b" * 40
        return original_write_for_run(self, **kwargs)

    monkeypatch.setattr(ContainerOutputSimulator, "write_for_run", write_mismatched_baseline)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unchanged\n", encoding="utf-8")
    await NodeAgentWorkerService.run_once(ctx["run_id"], db=db)

    run = (await db.execute(
        select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == ctx["run_id"])
    )).scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "integration_rejected_by_baseline"
    assert target.read_text(encoding="utf-8") == "unchanged\n"
    await assert_lock_released(db, ctx["node_id"])


@pytest.mark.asyncio
async def test_e2e_aggregate_validation_fails_and_rolls_back(
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = code_change_node("n1", files=["src/write.py"])
    node["write_set"] = ["src/write.py"]
    node["read_set"] = []
    node["conflict_contributions"] = [
        {
            "aggregate_target": "src/router.json",
            "contribution_path": ".bridle/aggregate/src/router.json/n1.json",
        }
    ]
    plan = {
        "goal": "aggregate fail",
        "aggregate_files": [
            {
                "target_path": "src/router.json",
                "contribution_dir": ".bridle/aggregate/src/router.json",
                "merge_strategy": "json_list",
                "owner": "main-agent",
                "contributors": ["n1"],
                "validation": {
                    "unique_key": "path",
                    "validation_commands": [f'{sys.executable} -c "import sys; sys.exit(1)"'],
                },
            }
        ],
        "nodes": [node],
    }
    ctx = await start_containerized_run(
        db=db,
        client=client,
        test_workspace=test_workspace,
        monkeypatch=monkeypatch,
        plan_json=plan,
    )
    router = test_workspace / "src" / "router.json"
    router.parent.mkdir(parents=True, exist_ok=True)
    router.write_text('{"items": []}\n', encoding="utf-8")
    write_target = test_workspace / "src" / "write.py"
    write_target.write_text("before\n", encoding="utf-8")

    await NodeAgentWorkerService.run_once(ctx["run_id"], db=db)

    run = (await db.execute(
        select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == ctx["run_id"])
    )).scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "aggregate_validation_failed"
    assert write_target.read_text(encoding="utf-8") == "before\n"
    assert router.read_text(encoding="utf-8") == '{"items": []}\n'
    await assert_lock_released(db, ctx["node_id"])


@pytest.mark.asyncio
async def test_e2e_container_health_failure(
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await start_containerized_run(
        db=db, client=client, test_workspace=test_workspace, monkeypatch=monkeypatch,
        disable_simulator=True,
    )

    def unhealthy_run(self, *, run_id: str, node_id: str, workspace_root: Path) -> dict:
        diag_dir = workspace_root / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / "health.error").write_text("forced unhealthy\n", encoding="utf-8")
        raise NodeContainerError(
            "container_health_failed",
            detail={"run_id": run_id, "container_id": "fake-unhealthy", "health": "unhealthy"},
        )

    with patch.object(NodeContainerOrchestrator, "run_node_container", unhealthy_run):
        await NodeAgentWorkerService.run_once(ctx["run_id"], db=db)

    run = (await db.execute(
        select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == ctx["run_id"])
    )).scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "container_health_failed"
    await assert_lock_released(db, ctx["node_id"])
    diag = test_workspace / ".aicoding" / "container-workspaces" / ctx["run_id"] / "diagnostics"
    assert (diag / "health.error").exists()
