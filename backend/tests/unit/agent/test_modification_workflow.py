"""Persistent project-modification workflow contract tests."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationState,
    ModificationWorkflow,
)
from bridle.api.errors import ConflictError
from bridle.features.project_map.service import ProjectMapService
from bridle.features.project_map.store import ProjectPlanStore
from tests.helpers.modification_workflow import freeze_test_contract_for_workflow


@pytest.fixture
def store(tmp_path) -> ProjectPlanStore:
    result = ProjectPlanStore(tmp_path, project_id="project-1")
    result.initialize(scan_if_created=False)
    return result


@pytest.fixture
def workflow(store: ProjectPlanStore) -> ModificationWorkflow:
    return ModificationWorkflow(store)


def _apply(
    workflow: ModificationWorkflow,
    node_id: str,
    event: ModificationEvent,
    sequence: int,
    *,
    expected_revision: int | None = None,
) -> dict:
    if (
        event is ModificationEvent.TEST_CONTRACT_APPROVED
        and workflow.active_test_contract(node_id) is None
    ):
        current = workflow.get(node_id)
        freeze_test_contract_for_workflow(
            workflow,
            node_id,
            int(current["revision"]),
        )
    return workflow.apply(
        node_id,
        event=event,
        event_id=f"{node_id}-{sequence}-{event.value}",
        expected_revision=expected_revision,
    )


def _advance_to_implementing(workflow: ModificationWorkflow, node_id: str) -> dict:
    events = (
        ModificationEvent.START,
        ModificationEvent.TEST_CONTRACT_APPROVED,
        ModificationEvent.RED_ALLOWED,
        ModificationEvent.RED_VERIFICATION_STARTED,
        ModificationEvent.RED_CONFIRMED,
        ModificationEvent.IMPLEMENTATION_STARTED,
    )
    result: dict = {}
    for sequence, event in enumerate(events, start=1):
        result = _apply(workflow, node_id, event, sequence)
    return result


def test_normal_red_green_flow_persists_every_transition(
    workflow: ModificationWorkflow,
    store: ProjectPlanStore,
) -> None:
    node_id = "node-normal"
    events = (
        ModificationEvent.START,
        ModificationEvent.TEST_CONTRACT_APPROVED,
        ModificationEvent.RED_ALLOWED,
        ModificationEvent.RED_VERIFICATION_STARTED,
        ModificationEvent.RED_CONFIRMED,
        ModificationEvent.IMPLEMENTATION_STARTED,
        ModificationEvent.SUBMITTED,
        ModificationEvent.FINAL_VERIFICATION_STARTED,
        ModificationEvent.FINAL_VERIFICATION_PASSED,
        ModificationEvent.PUBLISHED,
    )

    for sequence, event in enumerate(events, start=1):
        result = _apply(workflow, node_id, event, sequence)
        assert result["applied"] is True
        assert result["revision"] == sequence

    assert result["state"] == ModificationState.PUBLISHED.value
    history = store.list_modification_events(node_id)
    assert [item["event"] for item in history] == [item.value for item in events]
    assert history[-1]["to_state"] == ModificationState.PUBLISHED.value


def test_restart_recovers_last_persisted_state(store: ProjectPlanStore) -> None:
    first = ModificationWorkflow(store)
    _advance_to_implementing(first, "node-restart")

    reopened_store = ProjectPlanStore.open_existing(store.project_root)
    restarted = ModificationWorkflow(reopened_store)

    assert restarted.get("node-restart")["state"] == ModificationState.IMPLEMENTING.value
    submitted = _apply(restarted, "node-restart", ModificationEvent.SUBMITTED, 7)
    assert submitted["state"] == ModificationState.SUBMITTED.value


def test_duplicate_event_is_idempotent_and_not_a_second_audit_event(
    workflow: ModificationWorkflow,
    store: ProjectPlanStore,
) -> None:
    first = workflow.apply(
        "node-idempotent",
        event=ModificationEvent.START,
        event_id="same-event",
    )
    duplicate = workflow.apply(
        "node-idempotent",
        event=ModificationEvent.START,
        event_id="same-event",
    )

    assert first["applied"] is True
    assert duplicate == {**first, "applied": False}
    assert len(store.list_modification_events("node-idempotent")) == 1


def test_illegal_transition_cannot_skip_red(workflow: ModificationWorkflow) -> None:
    _apply(workflow, "node-illegal", ModificationEvent.START, 1)

    with pytest.raises(ConflictError) as captured:
        _apply(
            workflow,
            "node-illegal",
            ModificationEvent.IMPLEMENTATION_STARTED,
            2,
        )

    assert captured.value.api_error.code == "modification_transition_invalid"
    assert workflow.get("node-illegal")["state"] == ModificationState.TEST_AUTHORING.value


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        (ModificationEvent.INVALID_TEST, ModificationState.TEST_AUTHORING),
        (ModificationEvent.INFRASTRUCTURE_FAILED, ModificationState.RED_ALLOWED),
        (ModificationEvent.BASELINE_EXPIRED, ModificationState.TEST_AUTHORING),
    ],
)
def test_red_verification_failures_have_explicit_rollback_states(
    workflow: ModificationWorkflow,
    failure: ModificationEvent,
    expected_state: ModificationState,
) -> None:
    node_id = f"node-{failure.value}"
    for sequence, event in enumerate(
        (
            ModificationEvent.START,
            ModificationEvent.TEST_CONTRACT_APPROVED,
            ModificationEvent.RED_ALLOWED,
            ModificationEvent.RED_VERIFICATION_STARTED,
        ),
        start=1,
    ):
        _apply(workflow, node_id, event, sequence)

    result = _apply(workflow, node_id, failure, 5)

    assert result["state"] == expected_state.value


def test_final_failure_returns_to_implementing_then_resubmit_can_publish(
    workflow: ModificationWorkflow,
) -> None:
    node_id = "node-final-retry"
    _advance_to_implementing(workflow, node_id)
    _apply(workflow, node_id, ModificationEvent.SUBMITTED, 7)
    _apply(workflow, node_id, ModificationEvent.FINAL_VERIFICATION_STARTED, 8)

    failed = _apply(workflow, node_id, ModificationEvent.FINAL_VERIFICATION_FAILED, 9)
    assert failed["state"] == ModificationState.IMPLEMENTING.value

    _apply(workflow, node_id, ModificationEvent.SUBMITTED, 10)
    _apply(workflow, node_id, ModificationEvent.FINAL_VERIFICATION_STARTED, 11)
    ready = _apply(workflow, node_id, ModificationEvent.FINAL_VERIFICATION_PASSED, 12)
    published = _apply(workflow, node_id, ModificationEvent.PUBLISHED, 13)

    assert ready["state"] == ModificationState.READY_TO_PUBLISH.value
    assert published["state"] == ModificationState.PUBLISHED.value


def test_stale_revision_is_rejected_as_concurrent_transition_conflict(
    workflow: ModificationWorkflow,
) -> None:
    started = _apply(workflow, "node-concurrent", ModificationEvent.START, 1)
    assert started["revision"] == 1
    approved = _apply(
        workflow,
        "node-concurrent",
        ModificationEvent.TEST_CONTRACT_APPROVED,
        2,
        expected_revision=1,
    )
    assert approved["revision"] == 2

    with pytest.raises(ConflictError) as captured:
        _apply(
            workflow,
            "node-concurrent",
            ModificationEvent.BASELINE_EXPIRED,
            3,
            expected_revision=1,
        )

    assert captured.value.api_error.code == "modification_revision_conflict"
    assert workflow.get("node-concurrent")["revision"] == 2


@pytest.mark.asyncio
async def test_store_for_migrates_existing_plan_database(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = ProjectPlanStore(tmp_path, project_id="legacy-project")
    legacy.initialize(scan_if_created=False)
    with closing(sqlite3.connect(legacy.database_path)) as connection:
        connection.execute("DROP TABLE modification_events")
        connection.execute("DROP TABLE modification_workflows")
        connection.execute(
            "UPDATE metadata SET value = '3' WHERE key = 'schema_version'"
        )
        connection.commit()

    async def get_record(_db, _project_id: str):
        return SimpleNamespace(id="legacy-project", path=str(tmp_path))

    monkeypatch.setattr(
        "bridle.features.project_map.service.ProjectService.get_record",
        get_record,
    )

    reopened = await ProjectMapService.store_for(object(), "legacy-project")

    with closing(sqlite3.connect(reopened.database_path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        schema_version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        workflow_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(modification_workflows)"
            ).fetchall()
        }
    assert {
        "modification_workflows",
        "modification_events",
        "test_contracts",
        "verification_runs",
    } <= tables
    assert "test_contract_version" in workflow_columns
    assert schema_version == "8"
