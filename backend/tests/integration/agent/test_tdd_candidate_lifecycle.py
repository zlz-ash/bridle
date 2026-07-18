from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from bridle.agent.container.backend import AgentContainerBackend
from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.container.runner import FakeContainerRunner
from bridle.agent.container.test_backend import ModuleContainerTestBackend
from bridle.agent.runtime.change_outbox import (
    AtomicPatchCommitter,
    ChangeCorrelation,
    ChangeOutbox,
)
from bridle.agent.runtime.mailbox import AgentAddress
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor
import bridle.features.project_map.modify_loop_service as modify_loop_service
from bridle.features.project_map.store import ProjectPlanStore


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _evidence_payload(
    root: Path,
    *,
    setup,
    submission_id: str,
    classification: str,
    exit_code: int,
    changed_paths: list[str],
) -> dict:
    artifact = root / ".bridle" / "artifacts" / f"{classification.lower()}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"classification": classification}), encoding="utf-8")
    return {
        "run_id": setup.request.run_id,
        "node_id": setup.request.node_id,
        "candidate_id": setup.candidate_id,
        "submission_id": submission_id,
        "contract_version": "contract-e2e-v1",
        "test_code_hash": "test-code-e2e",
        "candidate_code_hash": "candidate-code-e2e",
        "required_command_ids": ["E2E-CALC"],
        "map_seq": setup.request.base_map_seq,
        "boundary_fingerprint": setup.boundary_fingerprint,
        "image_version": setup.request.image_version,
        "exit_code": exit_code,
        "duration_ms": 1,
        "classification": classification,
        "changed_paths": changed_paths,
        "artifact_ref": artifact.relative_to(root).as_posix(),
        "artifact_digest": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_full_tdd_candidate_lifecycle_with_durable_wait(test_workspace: Path) -> None:
    PlanNodeExecutionCoordinator = getattr(
        modify_loop_service,
        "PlanNodeExecutionCoordinator",
    )
    _write(test_workspace, "src/calc.py", "def add(a, b):\n    return a - b\n")
    _write(test_workspace, "tests/test_calc.py", "")

    project_id = "project-tdd-e2e"
    owner = AgentAddress(project_id, "main", 1)
    store = ProjectPlanStore(test_workspace, project_id=project_id)
    store.initialize()
    mailbox = PersistentMailbox(
        test_workspace / ".bridle" / "mail.db",
        project_id=project_id,
        consumer_id="main-e2e",
    )
    candidate_service = CandidateExecutionService(test_workspace)
    container_backend = AgentContainerBackend(
        test_workspace,
        runner=FakeContainerRunner(workspace_root=test_workspace),
    )
    started = asyncio.Event()

    async def model_driven_lifecycle(execution: dict) -> dict:
        started.set()
        setup = candidate_service.prepare_from_snapshot(
            {
                "module_id": "module-calc",
                "node_id": execution["node_id"],
                "implementation_entities": [
                    {"entity_id": "calc", "path": "src/calc.py"},
                ],
                "test_entities": [
                    {"entity_id": "test-calc", "path": "tests/test_calc.py"},
                ],
                "test_commands": ["python -m pytest tests/test_calc.py -q"],
                "interfaces": [],
            },
            run_id=execution["execution_id"],
            candidate_id="candidate-tdd-e2e",
            base_map_seq=1,
        )
        policy = SandboxPolicy.for_run(
            run_id=setup.request.run_id,
            node_id=setup.request.node_id,
            workspace_root=setup.workspace.project_dir,
            allowed_files=list(setup.workspace.write_set),
            node_tests=["python -m pytest tests/test_calc.py -q"],
            network_allowed=False,
            command_timeout_seconds=30,
        )
        test_backend = ModuleContainerTestBackend(
            container_backend,
            candidate_request=setup.request,
            candidate_root=str(setup.workspace.root),
            module_root=str(setup.workspace.module_root),
            candidate_rel=setup.workspace.candidate_rel,
            test_entity_id="test-calc",
            required_commands=["python -m pytest tests/test_calc.py -q"],
            required_command_ids=["E2E-CALC"],
            map_seq=1,
            red_verification=True,
        )
        tools = SandboxedToolExecutor(policy, test_backend=test_backend)

        red_test = "from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        red_write = await tools.run_command(
            "python -c \"from pathlib import Path; "
            f"Path('tests/test_calc.py').write_text({red_test!r}, encoding='utf-8')\""
        )
        assert red_write["status"] == "completed"
        assert red_write["exit_code"] == 0
        red = await test_backend.run_authoritative_tests(policy=policy)
        assert red["status"] == "failed"
        assert red["results"][0]["exit_code"] != 0
        store.append_evidence(
            node_id=execution["node_id"],
            event="red_verified",
            payload=_evidence_payload(
                test_workspace,
                setup=setup,
                submission_id="red-draft",
                classification="EXPECTED_RED",
                exit_code=red["results"][0]["exit_code"],
                changed_paths=["tests/test_calc.py"],
                ),
            )

        final_test_backend = ModuleContainerTestBackend(
            container_backend,
            candidate_request=setup.request,
            candidate_root=str(setup.workspace.root),
            module_root=str(setup.workspace.module_root),
            candidate_rel=setup.workspace.candidate_rel,
            test_entity_id="test-calc",
            required_commands=["python -m pytest tests/test_calc.py -q"],
            required_command_ids=["E2E-CALC"],
            map_seq=1,
            red_verification=False,
        )
        wrong_write = await tools.run_command(
            "python -c \"from pathlib import Path; "
            "Path('src/calc.py').write_text('def add(a, b):\\n    return a * b\\n', encoding='utf-8')\""
        )
        assert wrong_write["status"] == "completed"
        assert wrong_write["exit_code"] == 0
        first_final = await final_test_backend.run_authoritative_tests(policy=policy)
        assert first_final["status"] == "failed"
        assert first_final["results"][0]["exit_code"] != 0
        assert store.read_execution(execution["wait_id"])["state"] == "waiting"

        final_write = await tools.run_command(
            "python -c \"from pathlib import Path; "
            "Path('src/calc.py').write_text('def add(a, b):\\n    return a + b\\n', encoding='utf-8')\""
        )
        assert final_write["status"] == "completed"
        assert final_write["exit_code"] == 0
        final = await final_test_backend.run_authoritative_tests(policy=policy)
        assert final["status"] == "completed"
        assert final["results"][0]["exit_code"] == 0
        submission = candidate_service.freeze_submission(setup)
        assert candidate_service.validate_submission(setup, submission).status == "valid"

        changed_paths = ["src/calc.py", "tests/test_calc.py"]
        store.append_evidence(
            node_id=execution["node_id"],
            event="submission_frozen",
            payload=_evidence_payload(
                test_workspace,
                setup=setup,
                submission_id=submission.submission_id,
                classification="SUBMITTED",
                exit_code=0,
                changed_paths=changed_paths,
            ),
        )
        publish = AtomicPatchCommitter(
            ChangeOutbox(test_workspace, project_id=project_id)
        ).commit_many(
            [
                {
                    "path": relative,
                    "change_type": "modify",
                    "new_text": (setup.workspace.project_dir / relative).read_text(encoding="utf-8"),
                }
                for relative in changed_paths
            ],
            correlation=ChangeCorrelation(
                trace_id=execution["execution_id"],
                project_id=project_id,
                agent_id="model-e2e",
                generation=1,
            ),
        )
        assert publish.status == "ready"
        store.append_evidence(
            node_id=execution["node_id"],
            event="published",
            payload=_evidence_payload(
                test_workspace,
                setup=setup,
                submission_id=submission.submission_id,
                classification="PASSED",
                exit_code=0,
                changed_paths=changed_paths,
            ),
        )
        assert store.validate_evidence_chain(execution["node_id"])["valid"] is True
        assert store.refresh_code_paths(changed_paths)["refreshed_paths"] == changed_paths
        return {
            "outcome": "completed",
            "result_ref": ".bridle/artifacts/passed.json",
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
        stage_runner=model_driven_lifecycle,
    )
    waiting = await coordinator.execute_plan_node("node-tdd-e2e")
    assert waiting["state"] == "waiting"
    await asyncio.wait_for(started.wait(), timeout=1)
    assert store.read_execution(waiting["wait_id"])["state"] == "waiting"

    await coordinator.wait_for_idle()
    ended = store.read_execution(waiting["wait_id"])
    assert ended["state"] == "ended"
    assert ended["outcome"] == "completed"
    assert coordinator.forward_completion_mail() == 1
    assert coordinator.forward_completion_mail() == 0
    claimed = mailbox.claim(owner)
    assert claimed.envelope is not None
    assert claimed.envelope.message_type == "node-workflow-result"
    assert claimed.envelope.payload["wait_id"] == waiting["wait_id"]
    assert mailbox.claim(owner).status == "empty"
    assert (test_workspace / "src" / "calc.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return a + b\n"
    )
