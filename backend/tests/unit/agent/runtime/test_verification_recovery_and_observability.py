from __future__ import annotations

from pathlib import Path

import bridle.agent.runtime.verification_orchestrator as verification_orchestrator
from bridle.features.project_map.store import ProjectPlanStore


def test_failure_matrix_and_trace_timeline_survive_restart(test_workspace: Path) -> None:
    classify_workflow_failure = getattr(
        verification_orchestrator,
        "classify_workflow_failure",
    )
    expected = {
        "final_verification_failed": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "IMPLEMENTING",
            "outcome": None,
            "evidence_valid": True,
        },
        "test_contract_invalid": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "TEST_AUTHORING",
            "outcome": None,
            "evidence_valid": False,
        },
        "container_temporarily_unavailable": {
            "retryable": True,
            "max_attempts": 5,
            "target_state": None,
            "outcome": None,
            "evidence_valid": True,
        },
        "baseline_expired": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "TEST_AUTHORING",
            "outcome": None,
            "evidence_valid": False,
        },
        "boundary_changed": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "TEST_AUTHORING",
            "outcome": None,
            "evidence_valid": False,
        },
        "image_changed": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "TEST_AUTHORING",
            "outcome": None,
            "evidence_valid": False,
        },
        "container_boundary_violation": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "VERIFICATION_BLOCKED",
            "outcome": "blocked",
            "evidence_valid": False,
        },
        "candidate_publish_failed": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "READY_TO_PUBLISH",
            "outcome": "failed",
            "evidence_valid": True,
        },
        "mailbox_busy": {
            "retryable": True,
            "max_attempts": 5,
            "target_state": "DELIVERY_PENDING",
            "outcome": None,
            "evidence_valid": True,
        },
        "mailbox_capacity": {
            "retryable": True,
            "max_attempts": 5,
            "target_state": "DELIVERY_PENDING",
            "outcome": None,
            "evidence_valid": True,
        },
        "mail_delivery_rejected": {
            "retryable": False,
            "max_attempts": 1,
            "target_state": "COMPLETION_DELIVERY_FAILED",
            "outcome": None,
            "evidence_valid": True,
        },
        "verification_retry_exhausted": {
            "retryable": False,
            "max_attempts": 5,
            "target_state": "VERIFICATION_BLOCKED",
            "outcome": "blocked",
            "evidence_valid": False,
        },
    }
    assert {code: classify_workflow_failure(code) for code in expected} == expected

    store = ProjectPlanStore(test_workspace, project_id="project-observability")
    store.ensure_schema()
    store.record_stage_event(
        trace_id="trace-complete",
        node_id="node-observability",
        candidate_id="candidate-observability",
        submission_id="submission-observability",
        run_id="run-observability",
        wait_id="wait-observability",
        message_id=None,
        stage="contract_review",
        status="completed",
        attempt=1,
        started_at="2026-07-17T00:00:00+00:00",
        ended_at="2026-07-17T00:00:01+00:00",
        duration_ms=1000,
        error_code=None,
        retry_reason=None,
        artifact_ref=".bridle/artifacts/contract.json",
        cleanup_status=None,
        detail={"preview": "x" * 4096, "source_code": "drop", "diff": "drop", "secret": "drop"},
    )
    store.record_stage_event(
        trace_id="trace-complete",
        node_id="node-observability",
        candidate_id="candidate-observability",
        submission_id="submission-observability",
        run_id="run-observability",
        wait_id="wait-observability",
        message_id="message-observability",
        stage="cleanup",
        status="completed",
        attempt=1,
        started_at="2026-07-17T00:00:09+00:00",
        ended_at="2026-07-17T00:00:10+00:00",
        duration_ms=1000,
        error_code=None,
        retry_reason=None,
        artifact_ref=None,
        cleanup_status="completed",
        detail={"container_id": "container-observability"},
    )

    restarted = ProjectPlanStore(test_workspace, project_id="project-observability")
    timeline = restarted.list_stage_events("trace-complete")
    assert [item["stage"] for item in timeline] == ["contract_review", "cleanup"]
    assert timeline[0]["detail"] == {"preview": "x" * 2048}
    assert timeline[1]["message_id"] == "message-observability"
    assert timeline[1]["cleanup_status"] == "completed"
