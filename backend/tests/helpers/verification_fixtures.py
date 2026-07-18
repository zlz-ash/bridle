"""Shared test fixtures for real verification wiring."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from bridle.agent.container.candidate_contract import (
    FrozenTestContract,
    TestCaseSnapshot,
    TestCommandSnapshot,
    TestFileSnapshot,
)
from bridle.agent.container.container_control import (
    build_control_envelope,
    format_control_envelope_line,
)
from bridle.agent.container.runner import ContainerResult, FakeContainerRunner
from bridle.agent.container.test_command_compiler import TestCommandCompiler
from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationWorkflow,
)


class PassingStructuredRunner(FakeContainerRunner):
    """Return passing structured evidence for every frozen command in the active slot."""

    def __init__(self, workspace_root: str | Path) -> None:
        super().__init__(workspace_root)
        self.executions: list[dict[str, object]] = []

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        del command, timeout_seconds
        request, current = self._load(container_id)
        slot_root = Path(str(request.module_mount_root))
        test_request = json.loads(
            (slot_root / "diagnostics" / "test-request.json").read_text(
                encoding="utf-8"
            )
        )
        env = environment or {}
        candidate_rel = str(env.get("BRIDLE_CANDIDATE_REL") or "")
        if not candidate_rel:
            lease = json.loads(
                (slot_root / "diagnostics" / ".lease.json").read_text(
                    encoding="utf-8"
                )
            )
            candidate_rel = str(lease["candidate_rel"])
        results = [
            {
                "command_id": item["command_id"],
                "exit_code": 0,
                "stdout": "passed\n",
                "stderr": "",
                "case_results": [],
                "collection_errors": [],
            }
            for item in test_request["commands"]
        ]
        manifest = {
            "schema": "bridle.container_test_result/v1",
            "status": "completed",
            "exit_code": 0,
            "results": results,
        }
        envelope = build_control_envelope(
            manifest=manifest,
            run_id=str(env.get("BRIDLE_RUN_ID") or ""),
            candidate_rel=candidate_rel,
            exit_code=0,
        )
        stdout = format_control_envelope_line(envelope)
        result = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="running",
            network_mode=current.network_mode,
            health="healthy",
            finished_at=datetime.now(UTC),
            exit_code=0,
            stdout=stdout,
            stderr="",
        )
        self._containers[container_id] = (request, result)
        self._logs[container_id].append(stdout)
        self.executions.append(
            {
                "candidate_id": candidate_rel.rsplit("/", 1)[-1],
                "command_ids": [item["command_id"] for item in results],
                "executed_at": time.time(),
            }
        )
        return result


def freeze_contract_for_candidate_identity(
    workflow: ModificationWorkflow,
    node_id: str,
    *,
    project_root: Path,
    test_commands: list[str],
    test_paths: list[str],
    map_seq: int,
    boundary_fingerprint: str,
    image_version: str,
) -> FrozenTestContract:
    """Freeze the exact identity later rebuilt from a candidate manifest."""
    approved = TestCommandCompiler.compile_commands(
        test_commands=test_commands,
        test_entity_id=node_id,
        map_seq=map_seq,
    )
    files = tuple(
        TestFileSnapshot(
            path=path,
            sha256=hashlib.sha256((project_root / path).read_bytes()).hexdigest(),
        )
        for path in test_paths
    )
    cases = tuple(
        TestCaseSnapshot(
            case_id=f"case-{index}-{node_id}",
            node_id=f"{test_paths[min(index, len(test_paths) - 1)]}::test_target_{index}",
        )
        for index, _command in enumerate(approved)
    )
    contract = FrozenTestContract.freeze(
        test_files=files,
        cases=cases,
        commands=tuple(
            TestCommandSnapshot(
                command_id=item.command_id,
                argv=tuple(item.argv),
                raw_command=item.raw_command,
                test_entity_id=item.test_entity_id,
                map_seq=item.map_seq,
            )
            for item in approved
        ),
        expected_failure_case_ids=tuple(case.case_id for case in cases),
        baseline_hash=hashlib.sha256(
            "".join(item.sha256 for item in files).encode("utf-8")
        ).hexdigest(),
        map_seq=map_seq,
        boundary_fingerprint=boundary_fingerprint,
        image_version=image_version,
    )
    if workflow.current(node_id) is None:
        workflow.apply(
            node_id,
            event=ModificationEvent.START,
            event_id=f"setup:{node_id}:start",
        )
    workflow.freeze_test_contract(
        node_id,
        contract_version=contract.contract_version,
        snapshot=contract.to_dict(),
    )
    return contract


def advance_to_implementing(
    workflow: ModificationWorkflow,
    node_id: str,
    contract: FrozenTestContract,
) -> None:
    """Advance one persisted workflow through the already-proven red gate."""
    if workflow.current(node_id) is None:
        workflow.apply(
            node_id,
            event=ModificationEvent.START,
            event_id=f"setup:{node_id}:start",
        )
    if workflow.active_test_contract(node_id) is None:
        workflow.freeze_test_contract(
            node_id,
            contract_version=contract.contract_version,
            snapshot=contract.to_dict(),
        )
    for event in (
        ModificationEvent.TEST_CONTRACT_APPROVED,
        ModificationEvent.RED_ALLOWED,
        ModificationEvent.RED_VERIFICATION_STARTED,
        ModificationEvent.RED_CONFIRMED,
        ModificationEvent.IMPLEMENTATION_STARTED,
    ):
        workflow.apply(
            node_id,
            event=event,
            event_id=f"setup:{node_id}:{event.value}",
        )
