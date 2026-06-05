"""Load node-agent inputs from the mounted container directory."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bridle.schemas.proposal import AgentContext


@dataclass(frozen=True)
class NodeAgentInputs:
    agent_context: AgentContext
    run_id: str
    node_id: str
    plan_node_id: str
    baseline_revision: str
    read_set: list[str]

    @classmethod
    def from_dir(cls, inputs_dir: Path) -> NodeAgentInputs:
        path = inputs_dir / "context.json"
        if not path.exists():
            raise FileNotFoundError(f"missing context.json under {inputs_dir}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        ctx_payload = payload.get("agent_context") or payload
        return cls(
            agent_context=AgentContext.model_validate(ctx_payload),
            run_id=str(payload["run_id"]),
            node_id=str(payload["node_id"]),
            plan_node_id=str(payload.get("plan_node_id", payload["node_id"])),
            baseline_revision=str(payload["baseline_revision"]),
            read_set=list(payload.get("read_set") or []),
        )
