"""Classification tests for authoritative red verification evidence."""
from __future__ import annotations

import hashlib

import pytest

from bridle.agent.container.candidate_contract import (
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
from bridle.agent.container.red_classification import (
    RedClassification,
    classify_red_verification,
    event_for_red_classification,
)
from bridle.agent.runtime.modification_workflow import ModificationEvent

EXISTING_NODE = "tests/test_feature.py::test_existing_behavior"
REQUESTED_NODE = "tests/test_feature.py::test_requested_change"


def _case_id(node_id: str) -> str:
    return hashlib.sha256(f"test-entity:{node_id}".encode()).hexdigest()[:16]


def _contract() -> FrozenTestContract:
    command = "python -m pytest tests/test_feature.py -q"
    return FrozenTestContract.freeze(
        test_files=(FileSnapshot(path="tests/test_feature.py", sha256="test-hash"),),
        cases=(
            CaseSnapshot(case_id=_case_id(EXISTING_NODE), node_id=EXISTING_NODE),
            CaseSnapshot(case_id=_case_id(REQUESTED_NODE), node_id=REQUESTED_NODE),
        ),
        commands=(
            CommandSnapshot(
                command_id="command-1",
                argv=("python", "-m", "pytest", "tests/test_feature.py", "-q"),
                raw_command=command,
                test_entity_id="test-entity",
                map_seq=7,
            ),
        ),
        expected_failure_case_ids=(_case_id(REQUESTED_NODE),),
        baseline_hash="baseline-v1",
        map_seq=7,
        boundary_fingerprint="boundary-v1",
        image_version="image-v1",
    )


def _manifest(
    case_results: list[dict],
    *,
    error_code: str = "test_failed",
    timed_out: bool = False,
    collection_errors: list[dict] | None = None,
) -> dict:
    return {
        "status": "failed" if error_code else "completed",
        "error_code": error_code or None,
        "results": [
            {
                "command_id": "command-1",
                "exit_code": 1 if error_code else 0,
                "timed_out": timed_out,
                "case_results": case_results,
                "collection_errors": collection_errors or [],
            }
        ],
    }


def _case(node_id: str, outcome: str, *, failure_type: str | None = None) -> dict:
    return {
        "node_id": node_id,
        "outcome": outcome,
        "phase": "call",
        "failure_type": failure_type,
    }


def test_expected_assertion_red_is_the_only_result_that_confirms_red() -> None:
    result = classify_red_verification(
        _contract(),
        _manifest(
            [
                _case(EXISTING_NODE, "passed"),
                _case(REQUESTED_NODE, "failed", failure_type="AssertionError"),
            ]
        ),
    )

    assert result.classification is RedClassification.EXPECTED_RED
    assert result.failed_case_ids == (_case_id(REQUESTED_NODE),)
    assert event_for_red_classification(result) is ModificationEvent.RED_CONFIRMED


def test_unexpected_exception_is_not_treated_as_expected_red() -> None:
    result = classify_red_verification(
        _contract(),
        _manifest(
            [
                _case(EXISTING_NODE, "passed"),
                _case(REQUESTED_NODE, "failed", failure_type="RuntimeError"),
            ]
        ),
    )

    assert result.classification is RedClassification.UNEXPECTED_RED
    assert result.unexpected_case_ids == (_case_id(REQUESTED_NODE),)
    assert event_for_red_classification(result) is ModificationEvent.INVALID_TEST


def test_pytest_collection_failure_is_invalid_test() -> None:
    result = classify_red_verification(
        _contract(),
        _manifest(
            [],
            collection_errors=[
                {
                    "node_id": "tests/test_feature.py",
                    "message": "SyntaxError during collection",
                }
            ],
        ),
    )

    assert result.classification is RedClassification.INVALID_TEST
    assert result.error_code == "pytest_collection_failed"
    assert event_for_red_classification(result) is ModificationEvent.INVALID_TEST


@pytest.mark.parametrize(
    "error_code",
    [
        "container_wait_timeout",
        "container_exec_failed",
        "control_envelope_missing",
    ],
)
def test_container_and_timeout_failures_are_infrastructure_errors(error_code: str) -> None:
    result = classify_red_verification(
        _contract(),
        _manifest([], error_code=error_code, timed_out=error_code == "container_wait_timeout"),
    )

    assert result.classification is RedClassification.INFRA_ERROR
    assert result.error_code == error_code
    assert event_for_red_classification(result) is ModificationEvent.INFRASTRUCTURE_FAILED


def test_existing_baseline_failure_blocks_implementation() -> None:
    result = classify_red_verification(
        _contract(),
        _manifest(
            [
                _case(EXISTING_NODE, "failed", failure_type="AssertionError"),
                _case(REQUESTED_NODE, "failed", failure_type="AssertionError"),
            ]
        ),
    )

    assert result.classification is RedClassification.BASELINE_REGRESSION
    assert result.baseline_failed_case_ids == (_case_id(EXISTING_NODE),)
    assert event_for_red_classification(result) is ModificationEvent.BASELINE_EXPIRED


def test_green_or_missing_case_evidence_cannot_fabricate_expected_red() -> None:
    green = classify_red_verification(
        _contract(),
        _manifest(
            [
                _case(EXISTING_NODE, "passed"),
                _case(REQUESTED_NODE, "passed"),
            ],
            error_code="",
        ),
    )
    missing = classify_red_verification(_contract(), _manifest([]))

    assert green.classification is RedClassification.UNEXPECTED_RED
    assert green.error_code == "expected_red_missing"
    assert missing.classification is RedClassification.INVALID_TEST
    assert missing.error_code == "case_evidence_missing"
