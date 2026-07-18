"""Contract tests for frozen formal test requirements."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.container.candidate_contract import FrozenTestContract
from bridle.agent.container.test_command_compiler import TestCommandCompiler
from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationWorkflow,
)
from bridle.api.errors import ConflictError, ValidationError
from bridle.features.project_map.store import ProjectPlanStore

TEST_FILE = "tests/test_feature.py"
TEST_CASES = [
    f"{TEST_FILE}::test_existing_behavior",
    f"{TEST_FILE}::test_requested_change",
]
TEST_COMMANDS = [f"python -m pytest {TEST_FILE} -q"]


def _write_test(root: Path, content: str = "def test_requested_change():\n    assert False\n") -> None:
    target = root / TEST_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _contract(
    root: Path,
    *,
    test_cases: list[str] | None = None,
    test_commands: list[str] | None = None,
    expected_failure_cases: list[str] | None = None,
    baseline_hash: str = "baseline-v1",
    map_seq: int = 7,
    boundary_fingerprint: str = "boundary-v1",
    image_version: str = "image-v1",
) -> FrozenTestContract:
    return TestCommandCompiler.freeze_contract(
        project_root=root,
        test_files=[TEST_FILE],
        test_cases=test_cases if test_cases is not None else TEST_CASES,
        test_commands=test_commands if test_commands is not None else TEST_COMMANDS,
        expected_failure_cases=(
            expected_failure_cases
            if expected_failure_cases is not None
            else [f"{TEST_FILE}::test_requested_change"]
        ),
        test_entity_id="test-entity-1",
        baseline_hash=baseline_hash,
        map_seq=map_seq,
        boundary_fingerprint=boundary_fingerprint,
        image_version=image_version,
    )


def test_contract_serialization_and_ids_are_stable(test_workspace: Path) -> None:
    _write_test(test_workspace)

    first = _contract(test_workspace)
    second = _contract(test_workspace)
    restored = FrozenTestContract.from_dict(first.to_dict())

    assert first.to_json() == second.to_json()
    assert first.contract_version == second.contract_version
    assert [case.case_id for case in first.cases] == [case.case_id for case in second.cases]
    assert [command.command_id for command in first.commands] == [
        command.command_id for command in second.commands
    ]
    assert restored == first


def test_test_file_change_invalidates_frozen_contract(test_workspace: Path) -> None:
    _write_test(test_workspace)
    frozen = _contract(test_workspace)

    _write_test(test_workspace, "def test_requested_change():\n    assert True\n")
    current = _contract(test_workspace)

    assert frozen.diff(current) == ("test_files",)


def test_command_baseline_map_boundary_and_image_changes_invalidate_contract(
    test_workspace: Path,
) -> None:
    _write_test(test_workspace)
    frozen = _contract(test_workspace)

    variants = {
        "commands": _contract(
            test_workspace,
            test_commands=[*TEST_COMMANDS, "python -m pytest tests/test_other.py -q"],
        ),
        "baseline_hash": _contract(test_workspace, baseline_hash="baseline-v2"),
        "map_seq": _contract(test_workspace, map_seq=8),
        "boundary_fingerprint": _contract(test_workspace, boundary_fingerprint="boundary-v2"),
        "image_version": _contract(test_workspace, image_version="image-v2"),
    }

    for expected_reason, current in variants.items():
        assert expected_reason in frozen.diff(current)


def test_deleting_or_weakening_expected_failure_scope_is_rejected(
    test_workspace: Path,
) -> None:
    _write_test(test_workspace)

    with pytest.raises(ValueError, match="expected_failure_case_unknown"):
        _contract(test_workspace, test_cases=TEST_CASES[:1])
    with pytest.raises(ValueError, match="expected_failure_scope_required"):
        _contract(test_workspace, expected_failure_cases=[])


def test_only_current_frozen_contract_can_unlock_red_allowed(test_workspace: Path) -> None:
    _write_test(test_workspace)
    store = ProjectPlanStore(test_workspace, project_id="project-contract")
    store.initialize(scan_if_created=False)
    workflow = ModificationWorkflow(store)
    started = workflow.apply(
        "node-contract",
        event=ModificationEvent.START,
        event_id="contract:start",
    )

    with pytest.raises(ConflictError, match="frozen test contract"):
        workflow.apply(
            "node-contract",
            event=ModificationEvent.TEST_CONTRACT_APPROVED,
            event_id="contract:approve-without-contract",
            expected_revision=started["revision"],
        )

    frozen = _contract(test_workspace)
    store.freeze_test_contract(
        "node-contract",
        contract_version=frozen.contract_version,
        snapshot=frozen.to_dict(),
    )
    approved = workflow.apply(
        "node-contract",
        event=ModificationEvent.TEST_CONTRACT_APPROVED,
        event_id="contract:approve-v1",
        expected_revision=started["revision"],
    )
    assert approved["test_contract_version"] == frozen.contract_version

    restarted_store = ProjectPlanStore(test_workspace, project_id="project-contract")
    restarted_store.ensure_schema()
    assert restarted_store.get_active_test_contract("node-contract")["contract_version"] == (
        frozen.contract_version
    )

    restarted_store.invalidate_test_contract(
        "node-contract",
        contract_version=frozen.contract_version,
        reason="test_files_changed",
    )
    with pytest.raises(ConflictError, match="frozen test contract"):
        ModificationWorkflow(restarted_store).apply(
            "node-contract",
            event=ModificationEvent.RED_ALLOWED,
            event_id="contract:allow-invalidated",
            expected_revision=approved["revision"],
        )


def test_store_rejects_snapshot_content_that_does_not_match_version(
    test_workspace: Path,
) -> None:
    _write_test(test_workspace)
    frozen = _contract(test_workspace)
    tampered = frozen.to_dict()
    tampered["baseline_hash"] = "tampered-baseline"
    store = ProjectPlanStore(test_workspace, project_id="project-tampered-contract")
    store.initialize(scan_if_created=False)

    with pytest.raises(ValidationError, match="content hash"):
        store.freeze_test_contract(
            "node-tampered",
            contract_version=frozen.contract_version,
            snapshot=tampered,
        )
