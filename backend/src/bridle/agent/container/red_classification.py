"""Classify authoritative container evidence before workflow transitions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bridle.agent.container.candidate_contract import FrozenTestContract
from bridle.agent.runtime.modification_workflow import ModificationEvent


class RedClassification(StrEnum):
    EXPECTED_RED = "EXPECTED_RED"
    UNEXPECTED_RED = "UNEXPECTED_RED"
    INVALID_TEST = "INVALID_TEST"
    INFRA_ERROR = "INFRA_ERROR"
    BASELINE_REGRESSION = "BASELINE_REGRESSION"


@dataclass(frozen=True)
class RedClassificationResult:
    classification: RedClassification
    error_code: str
    failed_case_ids: tuple[str, ...] = ()
    unexpected_case_ids: tuple[str, ...] = ()
    baseline_failed_case_ids: tuple[str, ...] = ()
    collection_errors: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "error_code": self.error_code,
            "failed_case_ids": list(self.failed_case_ids),
            "unexpected_case_ids": list(self.unexpected_case_ids),
            "baseline_failed_case_ids": list(self.baseline_failed_case_ids),
            "collection_errors": [dict(item) for item in self.collection_errors],
        }


_INFRA_ERROR_CODES = {
    "active_slot_collect_failed",
    "baseline_or_mock_tampered",
    "container_exec_failed",
    "container_wait_timeout",
}
_INFRA_ERROR_PREFIXES = (
    "active_slot_",
    "container_",
    "control_",
)


def classify_red_verification(
    contract: FrozenTestContract,
    manifest: dict[str, Any],
) -> RedClassificationResult:
    """Classify one formal red run against the exact frozen test contract."""
    command_results = [
        dict(item) for item in manifest.get("results") or [] if isinstance(item, dict)
    ]
    manifest_error = str(manifest.get("error_code") or "")
    timed_out = any(bool(item.get("timed_out")) for item in command_results)
    if timed_out or _is_infrastructure_error(manifest_error):
        return RedClassificationResult(
            classification=RedClassification.INFRA_ERROR,
            error_code=manifest_error or "container_wait_timeout",
        )

    required_command_ids = {command.command_id for command in contract.commands}
    observed_command_ids = {
        str(item.get("command_id"))
        for item in command_results
        if item.get("command_id")
    }
    missing_command_ids = required_command_ids - observed_command_ids
    if missing_command_ids:
        return RedClassificationResult(
            classification=RedClassification.INVALID_TEST,
            error_code="required_command_evidence_missing",
            unexpected_case_ids=tuple(sorted(missing_command_ids)),
        )

    collection_errors = tuple(
        dict(error)
        for item in command_results
        for error in item.get("collection_errors") or []
        if isinstance(error, dict)
    )
    if collection_errors:
        return RedClassificationResult(
            classification=RedClassification.INVALID_TEST,
            error_code="pytest_collection_failed",
            collection_errors=collection_errors,
        )

    case_results = [
        dict(case)
        for item in command_results
        for case in item.get("case_results") or []
        if isinstance(case, dict)
    ]
    if not case_results:
        return RedClassificationResult(
            classification=RedClassification.INVALID_TEST,
            error_code="case_evidence_missing",
        )

    case_id_by_node = {case.node_id: case.case_id for case in contract.cases}
    evidence_by_case_id: dict[str, dict[str, Any]] = {}
    unexpected: set[str] = set()
    for case in case_results:
        node_id = str(case.get("node_id") or "")
        case_id = case_id_by_node.get(node_id)
        if case_id is None:
            if str(case.get("outcome") or "") not in {"passed", "skipped"}:
                unexpected.add(node_id or "unknown_case")
            continue
        evidence_by_case_id[case_id] = case

    contracted_ids = set(case_id_by_node.values())
    missing_ids = contracted_ids - set(evidence_by_case_id)
    if missing_ids:
        return RedClassificationResult(
            classification=RedClassification.INVALID_TEST,
            error_code="contract_case_not_collected",
            unexpected_case_ids=tuple(sorted(missing_ids)),
        )

    expected_ids = set(contract.expected_failure_case_ids)
    baseline_ids = contracted_ids - expected_ids
    baseline_failed = {
        case_id
        for case_id in baseline_ids
        if str(evidence_by_case_id[case_id].get("outcome") or "") != "passed"
    }
    if baseline_failed:
        return RedClassificationResult(
            classification=RedClassification.BASELINE_REGRESSION,
            error_code="baseline_test_failed",
            failed_case_ids=tuple(sorted(baseline_failed)),
            baseline_failed_case_ids=tuple(sorted(baseline_failed)),
        )

    failed_expected: set[str] = set()
    for case_id in expected_ids:
        evidence = evidence_by_case_id[case_id]
        outcome = str(evidence.get("outcome") or "")
        failure_type = str(evidence.get("failure_type") or "")
        if outcome == "failed" and failure_type == "AssertionError":
            failed_expected.add(case_id)
        elif outcome != "passed":
            unexpected.add(case_id)

    if unexpected:
        return RedClassificationResult(
            classification=RedClassification.UNEXPECTED_RED,
            error_code="unexpected_test_failure",
            failed_case_ids=tuple(sorted(failed_expected | unexpected)),
            unexpected_case_ids=tuple(sorted(unexpected)),
        )
    if not failed_expected:
        return RedClassificationResult(
            classification=RedClassification.UNEXPECTED_RED,
            error_code="expected_red_missing",
        )
    return RedClassificationResult(
        classification=RedClassification.EXPECTED_RED,
        error_code="expected_red",
        failed_case_ids=tuple(sorted(failed_expected)),
    )


def event_for_red_classification(result: RedClassificationResult) -> ModificationEvent:
    """Map classification to a rollback or confirmation event; only expected red advances."""
    return {
        RedClassification.EXPECTED_RED: ModificationEvent.RED_CONFIRMED,
        RedClassification.UNEXPECTED_RED: ModificationEvent.INVALID_TEST,
        RedClassification.INVALID_TEST: ModificationEvent.INVALID_TEST,
        RedClassification.INFRA_ERROR: ModificationEvent.INFRASTRUCTURE_FAILED,
        RedClassification.BASELINE_REGRESSION: ModificationEvent.BASELINE_EXPIRED,
    }[result.classification]


def _is_infrastructure_error(error_code: str) -> bool:
    return error_code in _INFRA_ERROR_CODES or error_code.startswith(_INFRA_ERROR_PREFIXES)
