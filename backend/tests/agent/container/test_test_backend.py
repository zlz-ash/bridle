"""Tests for module container test backend verification semantics."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridle.agent.container.candidate_contract import (
    CandidateExecutionRequest,
    FrozenTestContract,
)
from bridle.agent.container.candidate_contract import (
    TestCaseSnapshot as CaseSnapshot,
)
from bridle.agent.container.candidate_contract import (
    TestCommandSnapshot as CommandSnapshot,
)
from bridle.agent.container.candidate_contract import (
    TestFileSnapshot as FileSnapshot,
)
from bridle.agent.container.test_backend import ModuleContainerTestBackend
from bridle.agent.container.test_command_compiler import TestCommandCompiler
from bridle.agent.container.red_classification import (
    RedClassification,
    RedClassificationResult,
    event_for_red_classification,
)
from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationState,
    ModificationWorkflow,
)
from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.features.project_map.store import ProjectPlanStore


def _backend(
    required: list[str],
    *,
    entity_id: str = "node-1",
    map_seq: int = 3,
    test_contract: FrozenTestContract | None = None,
    red_verification: bool = False,
) -> ModuleContainerTestBackend:
    approved = TestCommandCompiler.compile_commands(
        test_commands=required,
        test_entity_id=entity_id,
        map_seq=map_seq,
    )
    request = CandidateExecutionRequest(
        candidate_id="cand-1",
        run_id="run-1",
        node_id=entity_id,
        project_root=Path("."),
        base_map_seq=map_seq,
        write_set=(),
        read_set=(),
        readonly_files=(),
        tests=tuple(required),
        timeout_seconds=60,
        network_allowed=False,
        module_id="mod-a",
    )
    return ModuleContainerTestBackend(
        MagicMock(),
        candidate_request=request,
        candidate_root="/tmp/cand",
        module_root="/tmp/mod",
        candidate_rel="candidates/cand-1",
        test_entity_id=entity_id,
        required_commands=required,
        required_command_ids=[cmd.command_id for cmd in approved],
        map_seq=map_seq,
        test_contract=test_contract,
        red_verification=red_verification,
    )


def _red_contract(command_id: str) -> FrozenTestContract:
    existing = CaseSnapshot(case_id="case-existing", node_id="tests/test_a.py::test_existing")
    requested = CaseSnapshot(case_id="case-requested", node_id="tests/test_a.py::test_requested")
    command = "python -m pytest tests/test_a.py -q"
    return FrozenTestContract.freeze(
        test_files=(FileSnapshot(path="tests/test_a.py", sha256="test-hash"),),
        cases=(existing, requested),
        commands=(
            CommandSnapshot(
                command_id=command_id,
                argv=("python", "-m", "pytest", "tests/test_a.py", "-q"),
                raw_command=command,
                test_entity_id="node-1",
                map_seq=3,
            ),
        ),
        expected_failure_case_ids=(requested.case_id,),
        baseline_hash="baseline-v1",
        map_seq=3,
        boundary_fingerprint="boundary-v1",
        image_version="image-v1",
    )


@pytest.mark.asyncio
async def test_partial_required_commands_stays_blocked() -> None:
    backend = _backend(["python -m pytest tests/test_a.py -q", "python -m pytest tests/test_b.py -q"])
    backend._backend.run_tests_in_candidate.return_value = {
        "manifest": {"status": "completed"},
        "test_results": [
            {
                "command_id": backend.required_command_ids[0],
                "exit_code": 0,
                "argv": ["python"],
                "raw_command": backend.required_commands[0],
            }
        ],
        "container_id": "fake-1",
        "container_reused": True,
    }
    policy = SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=Path("."),
        allowed_files=[],
        node_tests=backend.required_commands,
    )
    result = await backend.run_authoritative_tests(policy=policy)
    assert result["status"] == "completed"
    assert backend.evidence.all_required_passed is False


@pytest.mark.asyncio
async def test_all_required_commands_must_pass_for_ready() -> None:
    backend = _backend(["python -m pytest tests/test_a.py -q", "python -m pytest tests/test_b.py -q"])
    ids = backend.required_command_ids

    backend._backend.run_tests_in_candidate.return_value = {
        "manifest": {"status": "completed"},
        "test_results": [
            {
                "command_id": command_id,
                "exit_code": 0,
                "argv": ["python"],
                "raw_command": command,
            }
            for command_id, command in zip(ids, backend.required_commands, strict=True)
        ],
        "container_id": "fake-1",
        "container_reused": True,
    }
    policy = SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=Path("."),
        allowed_files=[],
        node_tests=backend.required_commands,
    )
    await backend.run_authoritative_tests(policy=policy)
    assert backend.evidence.all_required_passed is True


@pytest.mark.asyncio
async def test_frozen_contract_adds_authoritative_red_classification() -> None:
    command = "python -m pytest tests/test_a.py -q"
    approved = TestCommandCompiler.compile_commands(
        test_commands=[command],
        test_entity_id="node-1",
        map_seq=3,
    )
    backend = _backend(
        [command],
        test_contract=_red_contract(approved[0].command_id),
        red_verification=True,
    )
    backend._backend.run_tests_in_candidate.return_value = {
        "manifest": {"status": "failed", "error_code": "test_failed"},
        "test_results": [
            {
                "command_id": approved[0].command_id,
                "exit_code": 1,
                "argv": ["python"],
                "raw_command": command,
                "case_results": [
                    {
                        "node_id": "tests/test_a.py::test_existing",
                        "outcome": "passed",
                        "phase": "call",
                        "failure_type": None,
                    },
                    {
                        "node_id": "tests/test_a.py::test_requested",
                        "outcome": "failed",
                        "phase": "call",
                        "failure_type": "AssertionError",
                    },
                ],
                "collection_errors": [],
            }
        ],
        "container_id": "fake-red",
        "container_reused": True,
    }
    policy = SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=Path("."),
        allowed_files=[],
        node_tests=[command],
    )

    result = await backend.run_authoritative_tests(policy=policy)

    assert result["status"] == "failed"
    assert result["red_classification"]["classification"] == "EXPECTED_RED"
    assert result["red_classification"]["failed_case_ids"] == ["case-requested"]
    assert backend.evidence.red_classification == result["red_classification"]


@pytest.mark.asyncio
async def test_authoritative_verification_reexecutes_all_commands_after_exploration() -> None:
    commands = [
        "python -m pytest tests/test_a.py -q",
        "python -m pytest tests/test_b.py -q",
    ]
    backend = _backend(commands)
    ids = backend.required_command_ids

    def completed(command_id: str, raw_command: str) -> dict:
        return {
            "manifest": {"status": "completed"},
            "test_results": [
                {
                    "command_id": command_id,
                    "exit_code": 0,
                    "argv": ["python"],
                    "raw_command": raw_command,
                }
            ],
            "container_id": "fake-authoritative",
            "container_reused": False,
        }

    backend._backend.run_tests_in_candidate.return_value = {
        "manifest": {"status": "completed"},
        "test_results": [
            completed(ids[0], commands[0])["test_results"][0],
            completed(ids[1], commands[1])["test_results"][0],
        ],
        "container_id": "fake-authoritative",
        "container_reused": False,
    }
    authoritative_policy = SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=Path("."),
        allowed_files=[],
        node_tests=commands,
    )

    result = await backend.run_authoritative_tests(policy=authoritative_policy)

    calls = backend._backend.run_tests_in_candidate.call_args_list
    assert [call.kwargs["test_commands"] for call in calls] == [
        commands,
    ]
    assert calls[0].kwargs["replace_container"] is True
    assert result["status"] == "completed"
    assert backend.evidence.executed_command_ids == sorted(ids)
    assert backend.evidence.all_required_passed is True


@pytest.mark.asyncio
async def test_red_allowed_requires_target_failure_and_green_baseline(
    tmp_path: Path,
) -> None:
    command = "python -m pytest tests/test_a.py -q"
    approved = TestCommandCompiler.compile_commands(
        test_commands=[command],
        test_entity_id="node-1",
        map_seq=3,
    )
    command_id = approved[0].command_id

    async def classify(scenario: str) -> tuple[dict, str]:
        contract = _red_contract(command_id)
        backend = _backend(
            [command],
            test_contract=contract,
            red_verification=True,
        )
        existing_outcome = "failed" if scenario == "baseline_failed" else "passed"
        target_outcome = "passed" if scenario == "all_green" else "failed"
        manifest_error = "container_exec_failed" if scenario == "infra_failed" else None
        test_results = [] if scenario == "infra_failed" else [
            {
                "command_id": None if scenario == "missing_command_id" else command_id,
                "exit_code": 0 if scenario == "all_green" else 1,
                "argv": ["python"],
                "raw_command": command,
                "case_results": [
                    {
                        "node_id": "tests/test_a.py::test_existing",
                        "outcome": existing_outcome,
                        "phase": "call",
                        "failure_type": (
                            "AssertionError" if existing_outcome == "failed" else None
                        ),
                    },
                    {
                        "node_id": "tests/test_a.py::test_requested",
                        "outcome": target_outcome,
                        "phase": "call",
                        "failure_type": (
                            "AssertionError" if target_outcome == "failed" else None
                        ),
                    },
                ],
                "collection_errors": [],
            }
        ]
        backend._backend.run_tests_in_candidate.return_value = {
            "manifest": {
                "status": "completed" if scenario == "all_green" else "failed",
                "error_code": manifest_error or (
                    None if scenario == "all_green" else "test_failed"
                ),
            },
            "test_results": test_results,
            "container_id": f"fake-red-{scenario}",
            "container_reused": False,
        }
        policy = SandboxPolicy.for_run(
            run_id="run-1",
            node_id="node-1",
            workspace_root=Path("."),
            allowed_files=[],
            node_tests=[command],
        )
        result = await backend.run_authoritative_tests(policy=policy)
        classification = result["red_classification"]
        scenario_store = ProjectPlanStore(
            tmp_path / scenario,
            project_id=f"project-{scenario}",
        )
        scenario_store.initialize(scan_if_created=False)
        workflow = ModificationWorkflow(scenario_store)
        workflow.apply(
            "node-1",
            event=ModificationEvent.START,
            event_id=f"{scenario}:start",
        )
        workflow.freeze_test_contract(
            "node-1",
            contract_version=contract.contract_version,
            snapshot=contract.to_dict(),
        )
        for event in (
            ModificationEvent.TEST_CONTRACT_APPROVED,
            ModificationEvent.RED_ALLOWED,
            ModificationEvent.RED_VERIFICATION_STARTED,
        ):
            workflow.apply(
                "node-1",
                event=event,
                event_id=f"{scenario}:{event.value}",
            )
        mapped = event_for_red_classification(
            RedClassificationResult(
                classification=RedClassification(classification["classification"]),
                error_code=str(classification["error_code"]),
            )
        )
        state = workflow.apply(
            "node-1",
            event=mapped,
            event_id=f"{scenario}:{mapped.value}",
        )["state"]
        return result, state

    expected = {
        "expected_red": ("EXPECTED_RED", "expected_red", ModificationState.RED_CONFIRMED),
        "all_green": ("UNEXPECTED_RED", "expected_red_missing", ModificationState.TEST_AUTHORING),
        "baseline_failed": (
            "BASELINE_REGRESSION",
            "baseline_test_failed",
            ModificationState.TEST_AUTHORING,
        ),
        "infra_failed": (
            "INFRA_ERROR",
            "container_exec_failed",
            ModificationState.RED_ALLOWED,
        ),
        "missing_command_id": (
            "INVALID_TEST",
            "required_command_evidence_missing",
            ModificationState.TEST_AUTHORING,
        ),
    }
    for scenario, (classification, error_code, state) in expected.items():
        result, workflow_state = await classify(scenario)
        assert result["red_classification"]["classification"] == classification
        assert result["red_classification"]["error_code"] == error_code
        assert workflow_state == state.value
