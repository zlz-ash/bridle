"""Master agent skill assignment persisted at node selection time."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridle.config import get_config
from bridle.engine.skill_registry import build_skill_guidance_for_task, detect_env_layout
from bridle.logging.jsonl import log_event
from bridle.models.node import NodeRecord

logger = logging.getLogger("bridle.master_skill")


def _assignments_dir() -> Path:
    path = get_config().aicoding_dir / "skill-assignments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assign_skill_for_node(node: NodeRecord) -> dict[str, Any]:
    """Build master-issued skill assignment when selecting a child node."""
    node_tests = node.tests if isinstance(node.tests, list) else []
    files = list(node.files) if isinstance(node.files, list) else []
    effective_type = "code_change" if node.node_type == "micro" else node.node_type
    env_layout = detect_env_layout(
        node_type=effective_type,
        files=files,
        description=node.goal or "",
    )
    stack = _stack_from_layout(env_layout, effective_type)
    guidance = build_skill_guidance_for_task(
        requires_testing=bool(node_tests),
        stack=stack,
        env_layout=env_layout,
        assigned_by="master",
        assignment_reason=(
            f"Master selected node {node.plan_node_id} with tests={bool(node_tests)} "
            f"layout={env_layout}"
        ),
    )
    return guidance


def _stack_from_layout(env_layout: str, node_type: str | None) -> str | None:
    if env_layout in {"python_flat", "python_src_package"}:
        return "python"
    if env_layout == "java_spring":
        return "java"
    if node_type and str(node_type).lower() in {"python", "py"}:
        return "python"
    if node_type and str(node_type).lower() in {"java", "spring", "kotlin"}:
        return "java"
    return None


def persist_assignment(run_id: str, assignment: dict[str, Any]) -> Path:
    payload = {
        **assignment,
        "assigned_at": datetime.now(timezone.utc).isoformat(),
        "assigned_by": "master",
    }
    path = _assignments_dir() / f"{run_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event(
        "master_skill_assigned",
        "completed",
        run_id=run_id,
        detail={
            "use_skill": payload.get("use_skill"),
            "skill_id": payload.get("skill_id"),
            "submodule": payload.get("submodule"),
            "env_layout": payload.get("env_layout"),
        },
    )
    logger.info(
        "master_skill_assignment run_id=%s skill=%s submodule=%s",
        run_id,
        payload.get("skill_id"),
        payload.get("submodule"),
    )
    return path


def load_assignment(run_id: str) -> dict[str, Any] | None:
    path = _assignments_dir() / f"{run_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("master_skill_assignment_load_failed run_id=%s err=%s", run_id, exc)
        return None
