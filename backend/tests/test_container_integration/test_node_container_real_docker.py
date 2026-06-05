"""Real docker smoke for node-agent (gated by BRIDLE_RUN_DOCKER_TESTS)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bridle.engine.container_workspace import ContainerWorkspaceBuilder
from bridle.schemas.proposal import AgentContext
from bridle.services.node_container_orchestrator import NodeContainerOrchestrator
from bridle.services.worker_context_dumper import WorkerContextDumper
from bridle.models.node import NodeRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.engine.container_runner import LocalContainerRuntimeRunner

pytestmark = pytest.mark.skipif(
    not os.getenv("BRIDLE_RUN_DOCKER_TESTS"),
    reason="needs docker",
)


def test_node_container_writes_manifest(
    docker_container_runner: LocalContainerRuntimeRunner,
    test_workspace: Path,
    require_docker_images: None,
    require_agent_api_key: None,
) -> None:
  run_id = "docker-smoke-run"
  node = NodeRecord(
      id="node-db",
      plan_id="plan-1",
      plan_node_id="n-add",
      title="add",
      goal="Write add.py with def add(a,b): return a+b. No tests required.",
      node_type="code_change",
      status="ready",
      files=["add.py"],
      tests=[],
  )
  run = NodeAgentRunRecord(
      id=run_id,
      session_id="sess-1",
      node_id=node.id,
      plan_node_id=node.plan_node_id,
      status="queued",
      phase="initializing",
      attempt=1,
  )
  workspace = ContainerWorkspaceBuilder(test_workspace).build_node_workspace(
      run_id=run_id,
      node_id=node.id,
      read_set=[],
      write_set=["add.py"],
      readonly_context=[],
      interfaces={},
      tests=[],
      metrics={},
      conflict_contributions=[],
  )
  ctx = AgentContext(
      instruction="Write add.py with def add(a, b): return a + b",
      node={"id": "n-add", "title": "add", "goal": node.goal, "node_type": "code_change", "depends_on": []},
      allowed_files=["add.py"],
      tests=[],
  )
  WorkerContextDumper.dump(
      ctx=ctx,
      node=node,
      run=run,
      workspace_root=test_workspace,
      target_dir=workspace.root,
      baseline_revision="c" * 40,
      read_set=[],
  )
  assert not (workspace.root / "inputs" / "workspace_snapshot").exists()

  orchestrator = NodeContainerOrchestrator(test_workspace, runner=docker_container_runner)
  result = orchestrator.run_node_container(
      run_id=run_id,
      node_id=node.id,
      workspace_root=workspace.root,
  )
  assert result["exit_code"] == 0, result
  assert result.get("container_id")

  manifest_path = workspace.root / "output" / "manifest.json"
  assert manifest_path.exists()
  manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
  assert manifest["run_id"] == run_id
  assert manifest.get("status") == "completed"
  assert (workspace.root / "workspace" / "write" / "add.py").exists()
  content = (workspace.root / "workspace" / "write" / "add.py").read_text(encoding="utf-8")
  assert "def add" in content
