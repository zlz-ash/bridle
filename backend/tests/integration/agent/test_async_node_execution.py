from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bridle.agent.runtime.mailbox import AgentAddress
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
import bridle.features.project_map.modify_loop_service as modify_loop_service
from bridle.features.project_map.store import ProjectPlanStore


@pytest.mark.asyncio
async def test_execute_plan_node_returns_durable_wait_and_recovers(
    test_workspace: Path,
) -> None:
    PlanNodeExecutionCoordinator = getattr(
        modify_loop_service,
        "PlanNodeExecutionCoordinator",
    )
    project_id = "project-async-node"
    owner = AgentAddress(project_id, "main", 1)
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.ensure_schema()
    mailbox = PersistentMailbox(
        test_workspace / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id="main-consumer",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def stage_runner(execution: dict) -> dict:
        started.set()
        await release.wait()
        return {
            "outcome": "completed",
            "result_ref": ".bridle/results/async-node.json",
            "phases": [
                "map_check",
                "test_authoring",
                "contract_review",
                "red_verification",
                "implementation",
                "final_verification",
                "read_only_review",
                "conflict_check",
                "atomic_publish",
                "code_changed",
                "map_refresh",
            ],
        }

    coordinator = PlanNodeExecutionCoordinator(
        store,
        mailbox,
        owner=owner,
        stage_runner=stage_runner,
    )
    waiting = await coordinator.execute_plan_node("node-async")
    duplicate = await coordinator.execute_plan_node("node-async")

    assert waiting == duplicate
    assert waiting["state"] == "waiting"
    assert waiting["node_id"] == "node-async"
    assert {"wait_id", "execution_id", "revision"} <= waiting.keys()
    await asyncio.wait_for(started.wait(), timeout=1)
    assert store.read_execution(waiting["wait_id"])["state"] == "waiting"

    release.set()
    await coordinator.wait_for_idle()
    ended = store.read_execution(waiting["wait_id"])
    assert ended["state"] == "ended"
    assert ended["outcome"] == "completed"
    assert ended["phase"] == "map_refresh"

    assert coordinator.forward_completion_mail() == 1
    assert coordinator.forward_completion_mail() == 0
    claimed = mailbox.claim(owner)
    assert claimed.status == "claimed"
    assert claimed.envelope is not None
    assert claimed.envelope.message_type == "node-workflow-result"
    assert claimed.envelope.payload == {
        "wait_id": waiting["wait_id"],
        "execution_id": waiting["execution_id"],
        "node_id": "node-async",
        "state": "ended",
        "outcome": "completed",
        "result_ref": ".bridle/results/async-node.json",
        "revision": 1,
    }

    recovery = store.create_node_execution(
        node_id="node-recover",
        owner_address=owner.to_uri(),
    )

    async def recovered_runner(execution: dict) -> dict:
        return {
            "outcome": "cancelled",
            "result_ref": ".bridle/results/recovered.json",
            "phases": ["map_check"],
        }

    restarted = PlanNodeExecutionCoordinator(
        ProjectPlanStore(test_workspace, project_id=project_id),
        mailbox,
        owner=owner,
        stage_runner=recovered_runner,
    )
    assert await restarted.recover() == [recovery["execution_id"]]
    await restarted.wait_for_idle()
    assert store.read_execution(recovery["wait_id"])["outcome"] == "cancelled"
    assert restarted.forward_completion_mail() == 1
