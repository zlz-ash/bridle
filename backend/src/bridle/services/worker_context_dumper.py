"""Dump node-agent container inputs before docker run."""
from __future__ import annotations

import json
from pathlib import Path

from bridle.logging.jsonl import log_event
from bridle.models.node import NodeRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.schemas.proposal import AgentContext


class WorkerContextDumper:
    @staticmethod
    def dump(
        *,
        ctx: AgentContext,
        node: NodeRecord,
        run: NodeAgentRunRecord,
        workspace_root: Path,
        target_dir: Path,
        baseline_revision: str,
        read_set: list[str],
    ) -> Path:
        """Write inputs/context.json (read_set files live under workspace/read from ContainerWorkspaceBuilder)."""
        _ = workspace_root  # kept for call-site compatibility
        root = target_dir.resolve()
        inputs_dir = root / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)

        payload = {
            "agent_context": ctx.model_dump(),
            "run_id": run.id,
            "node_id": run.node_id,
            "plan_node_id": node.plan_node_id,
            "baseline_revision": baseline_revision,
            "read_set": list(read_set),
        }
        context_path = inputs_dir / "context.json"
        context_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        log_event(
            "worker_context_dumped",
            "completed",
            run_id=run.id,
            node_id=run.node_id,
            detail={"target_dir": str(root), "read_set_count": len(read_set)},
        )
        return context_path
