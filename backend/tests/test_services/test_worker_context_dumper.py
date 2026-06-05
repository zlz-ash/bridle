"""Tests for WorkerContextDumper."""
from __future__ import annotations

import json
from pathlib import Path

from bridle.models.node import NodeRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.schemas.proposal import AgentContext
from bridle.services.worker_context_dumper import WorkerContextDumper


def test_dump_writes_context_only_no_snapshot_dir(test_workspace: Path) -> None:
    node = NodeRecord(
        id="node-db-1",
        plan_id="plan-1",
        plan_node_id="n1",
        title="util",
        goal="read util",
        node_type="code",
        status="ready",
    )
    run = NodeAgentRunRecord(
        id="run-dump-1",
        session_id="sess-1",
        node_id=node.id,
        plan_node_id=node.plan_node_id,
        status="queued",
        phase="initializing",
        attempt=1,
    )
    ctx = AgentContext(
        instruction="implement",
        node={"id": "n1", "title": "util", "goal": "read", "node_type": "code", "depends_on": []},
        allowed_files=["out.py"],
        tests=[],
    )
    target = test_workspace / ".aicoding" / "container-workspaces" / run.id

    WorkerContextDumper.dump(
        ctx=ctx,
        node=node,
        run=run,
        workspace_root=test_workspace,
        target_dir=target,
        baseline_revision="b" * 40,
        read_set=["lib/util.py", "missing.py"],
    )

    inputs_dir = target / "inputs"
    assert inputs_dir.is_dir()
    assert list(inputs_dir.iterdir()) == [inputs_dir / "context.json"]
    assert not (inputs_dir / "workspace_snapshot").exists()

    context = json.loads((inputs_dir / "context.json").read_text(encoding="utf-8"))
    assert context["run_id"] == run.id
    assert context["plan_node_id"] == "n1"
    assert context["read_set"] == ["lib/util.py", "missing.py"]
