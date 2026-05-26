"""Nodes API router."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.api.errors import ConflictError, NotFoundError, ValidationError
from bridle.services.node_service import NodeService
from bridle.services.plan_service import PlanService

router = APIRouter(tags=["nodes"])


@router.get("/nodes/{node_id}")
async def get_node(node_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Get node details — only for nodes in the current active plan."""
    node = await NodeService.get_by_id(db, node_id)
    if node is None:
        raise NotFoundError(resource="node", message="Node not found (not in current plan or does not exist)")
    return node.model_dump()


@router.post("/nodes/{node_id}/run")
async def run_node(node_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Execute a node — only allowed for nodes in the current active plan."""
    from bridle.engine.blocker import Blocker
    from bridle.engine.collector import Collector
    from bridle.engine.sandbox_policy import SandboxPolicy
    from bridle.engine.sandboxed_tool_executor import (
        SandboxedToolExecutor,
        sandbox_results_to_command_results,
    )
    from bridle.models.node import NodeRecord as NR
    from bridle.services.evidence_service import EvidenceService
    from bridle.services.run_service import RunService

    from sqlalchemy import select

    # Get node record (must be in an active plan)
    node_record = await NodeService.get_record_by_id(db, node_id)
    if node_record is None:
        raise NotFoundError(resource="node", message="Node not found")

    # Verify node belongs to the current active plan
    current_plan = await PlanService.get_current(db)
    if current_plan is None or node_record.plan_id != current_plan.id:
        raise NotFoundError(resource="node", message="Node does not belong to the current active plan")

    # Get completed node IDs in the same plan (use plan_node_id to match depends_on)
    plan_result = await db.execute(select(NR).where(NR.plan_id == node_record.plan_id))
    plan_nodes = plan_result.scalars().all()
    completed_ids = {n.plan_node_id for n in plan_nodes if n.status == "completed"}

    # Check blockers
    block_result = Blocker.check(node_record, completed_ids)
    if block_result.blocked:
        raise ConflictError(
            resource="node",
            message=f"Node blocked: {block_result.reason}",
            details={"node_id": node_id, "reason": block_result.reason},
        )

    # Execute
    from bridle.config import get_config

    run = await RunService.create(db, node_id)

    node_record.status = "running"
    await db.commit()

    policy = SandboxPolicy.for_run(
        run_id=run.id,
        node_id=node_id,
        workspace_root=get_config().workspace,
        allowed_files=node_record.files if isinstance(node_record.files, list) else [],
        node_tests=node_record.tests if isinstance(node_record.tests, list) else [],
    )
    sandbox_result = await SandboxedToolExecutor(policy).run_allowed_tests(
        node_record.tests if isinstance(node_record.tests, list) else [],
    )
    cmd_results = sandbox_results_to_command_results(sandbox_result)

    # Update run
    last_result = cmd_results[-1] if cmd_results else {"exit_code": -1, "duration_ms": 0}
    await RunService.complete(
        db,
        run.id,
        exit_code=last_result["exit_code"],
        duration_ms=sum(r["duration_ms"] for r in cmd_results),
        stdout_path=last_result.get("stdout_path"),
        stderr_path=last_result.get("stderr_path"),
    )

    # Collect evidence
    evidences = Collector.collect_for_node(node_record, cmd_results)
    for ev_data in evidences:
        await EvidenceService.create(
            db,
            run_id=run.id,
            node_id=node_id,
            evidence_type=ev_data["evidence_type"],
            content=ev_data["content"],
            status=ev_data["status"],
        )

    # Update node status
    all_passed = all(r["exit_code"] == 0 for r in cmd_results)
    has_missing = any(ev["status"] == "missing_evidence" for ev in evidences)
    has_failed = any(ev["status"] == "failed" for ev in evidences)
    if all_passed and not has_missing:
        node_record.status = "completed"
    elif has_failed:
        node_record.status = "failed"
    elif has_missing:
        node_record.status = "missing_evidence"
    else:
        node_record.status = "completed"
    await db.commit()

    return {"run_id": run.id, "node_id": node_id, "status": node_record.status}


@router.get("/nodes/{node_id}/runs")
async def get_node_runs(node_id: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    from bridle.services.node_agent_run_service import NodeAgentRunService
    from bridle.services.run_service import RunService

    legacy_runs = [r.model_dump() for r in await RunService.list_by_node(db, node_id)]
    agent_runs = []
    for run in await NodeAgentRunService.list_by_node(db, node_id):
        payload = run.model_dump()
        payload["id"] = payload.pop("run_id")
        payload["container_logs"] = (
            [payload["container_logs_summary"]] if payload.get("container_logs_summary") else None
        )
        agent_runs.append(payload)
    return agent_runs + legacy_runs
