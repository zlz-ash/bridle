"""Contract tests for unified observability/log field naming."""
from __future__ import annotations

from bridle.observability.schema import (
    CORE_EVENT_PREFIXES,
    STANDARD_IDENTITY_FIELDS,
    STANDARD_RESULT_FIELDS,
)


class TestObservabilityContract:
    def test_standard_identity_fields(self) -> None:
        expected = {
            "session_id",
            "run_id",
            "node_id",
            "plan_id",
            "proposal_id",
        }
        assert expected.issubset(STANDARD_IDENTITY_FIELDS)

    def test_standard_result_fields(self) -> None:
        expected = {"error_code", "duration_ms", "exit_code", "timed_out"}
        assert expected.issubset(STANDARD_RESULT_FIELDS)

    def test_core_event_prefixes(self) -> None:
        expected = {
            "project_session.",
            "agent.",
            "model.",
            "tool.",
            "workspace.",
        }
        assert expected == set(CORE_EVENT_PREFIXES)
