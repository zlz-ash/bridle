"""Test-only setup helpers for the persistent modification workflow."""
from __future__ import annotations

import hashlib

from bridle.agent.container.candidate_contract import (
    FrozenTestContract,
    TestCaseSnapshot,
    TestCommandSnapshot,
    TestFileSnapshot,
)


def _frozen_test_contract(node_id: str, revision: int) -> FrozenTestContract:
    case_node_id = f"tests/test_contract.py::test_{node_id}"
    case_id = hashlib.sha256(f"test-entity:{case_node_id}".encode()).hexdigest()[:16]
    command = f"python -m pytest {case_node_id} -q"
    command_id = hashlib.sha256(f"test-entity:{revision}:{command}".encode()).hexdigest()[:16]
    content_hash = hashlib.sha256(f"{node_id}:{revision}".encode()).hexdigest()
    return FrozenTestContract.freeze(
        test_files=(TestFileSnapshot(path="tests/test_contract.py", sha256=content_hash),),
        cases=(TestCaseSnapshot(case_id=case_id, node_id=case_node_id),),
        commands=(
            TestCommandSnapshot(
                command_id=command_id,
                argv=("python", "-m", "pytest", case_node_id, "-q"),
                raw_command=command,
                test_entity_id="test-entity",
                map_seq=revision,
            ),
        ),
        expected_failure_case_ids=(case_id,),
        baseline_hash=content_hash,
        map_seq=revision,
        boundary_fingerprint="test-boundary",
        image_version="test-image",
    )


def freeze_test_contract_for_workflow(
    workflow,
    node_id: str,
    revision: int,
) -> FrozenTestContract:
    """Freeze a deterministic contract through the same API production uses."""
    active = workflow.active_test_contract(node_id)
    if active is not None:
        return FrozenTestContract.from_dict(active["snapshot"])
    contract = _frozen_test_contract(node_id, revision)
    workflow.freeze_test_contract(
        node_id,
        contract_version=contract.contract_version,
        snapshot=contract.to_dict(),
    )
    return contract
