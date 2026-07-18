"""State-driven authoritative verification orchestration tests."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import deque
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationState,
    ModificationWorkflow,
)
from bridle.agent.runtime.verification_orchestrator import (
    CandidateVerificationExecutor,
    TemporaryVerificationUnavailable,
    VerificationOrchestrator,
    VerificationResult,
)
from bridle.features.project_map.store import ProjectPlanStore
from tests.helpers.modification_workflow import freeze_test_contract_for_workflow


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 17, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _Executor:
    def __init__(self, *outcomes: VerificationResult | BaseException) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[dict] = []

    async def execute(self, *, run: dict, contract) -> VerificationResult:
        self.calls.append({"run": dict(run), "contract_version": contract.contract_version})
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _BlockingExecutor(_Executor):
    def __init__(self, outcome: VerificationResult) -> None:
        super().__init__(outcome)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, *, run: dict, contract) -> VerificationResult:
        self.started.set()
        await self.release.wait()
        return await super().execute(run=run, contract=contract)


class _CountingBlockingExecutor:
    def __init__(self, outcome: VerificationResult) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, *, run: dict, contract) -> VerificationResult:
        self.calls.append({"run": dict(run), "contract_version": contract.contract_version})
        self.started.set()
        await self.release.wait()
        return self.outcome


def _event(
    workflow: ModificationWorkflow,
    node_id: str,
    event: ModificationEvent,
    number: int,
    *,
    payload: dict | None = None,
) -> dict:
    return workflow.apply(
        node_id,
        event=event,
        event_id=f"setup:{node_id}:{number}:{event.value}",
        payload=payload,
    )


def _red_allowed(workflow: ModificationWorkflow, node_id: str) -> str:
    _event(workflow, node_id, ModificationEvent.START, 1)
    contract = freeze_test_contract_for_workflow(workflow, node_id, 1)
    _event(workflow, node_id, ModificationEvent.TEST_CONTRACT_APPROVED, 2)
    _event(workflow, node_id, ModificationEvent.RED_ALLOWED, 3)
    return contract.contract_version


def _submitted(
    workflow: ModificationWorkflow,
    node_id: str,
    *,
    candidate_id: str | None = None,
) -> str:
    contract_version = _red_allowed(workflow, node_id)
    for number, event in enumerate(
        (
            ModificationEvent.RED_VERIFICATION_STARTED,
            ModificationEvent.RED_CONFIRMED,
            ModificationEvent.IMPLEMENTATION_STARTED,
            ModificationEvent.SUBMITTED,
        ),
        start=4,
    ):
        _event(
            workflow,
            node_id,
            event,
            number,
            payload=(
                {"candidate_id": candidate_id}
                if event == ModificationEvent.SUBMITTED and candidate_id is not None
                else None
            ),
        )
    return contract_version


@pytest.fixture
def store(tmp_path) -> ProjectPlanStore:
    result = ProjectPlanStore(tmp_path, project_id="project-verification")
    result.initialize(scan_if_created=False)
    return result


@pytest.mark.asyncio
async def test_red_allowed_automatically_runs_authoritative_red_once(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    contract_version = _red_allowed(workflow, "node-red")
    executor = _Executor(
        VerificationResult(
            event=ModificationEvent.RED_CONFIRMED,
            status="expected_red",
            error_code="expected_red",
            summary={"failed_case_ids": ["CASE-target"]},
        )
    )
    orchestrator = VerificationOrchestrator(store, executor)

    result = await orchestrator.reconcile_node("node-red")
    duplicate = await orchestrator.reconcile_node("node-red")

    assert result["state"] == "completed"
    assert duplicate == result
    assert workflow.get("node-red")["state"] == ModificationState.RED_CONFIRMED.value
    assert len(executor.calls) == 1
    assert executor.calls[0]["run"]["phase"] == "red"
    assert executor.calls[0]["contract_version"] == contract_version
    events = workflow.events("node-red")
    assert [item["event"] for item in events].count("red_verification_started") == 1
    assert [item["event"] for item in events].count("red_confirmed") == 1


@pytest.mark.asyncio
async def test_submitted_automatically_runs_final_verification_without_model_tool_call(
    store: ProjectPlanStore,
) -> None:
    source = store.project_root / "src" / "final.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file = store.project_root / "tests" / "test_final.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_final():\n    assert True\n", encoding="utf-8")
    setup = CandidateExecutionService(store.project_root).prepare_from_snapshot(
        {
            "module_id": "module-final",
            "node_id": "node-final",
            "implementation_entities": [
                {"entity_id": "impl-final", "path": "src/final.py"}
            ],
            "test_entities": [
                {"entity_id": "test-final", "path": "tests/test_final.py"}
            ],
            "test_commands": ["python -m pytest tests/test_final.py -q"],
            "interfaces": [],
            "test_dir": "tests",
        },
        run_id="setup-final",
        candidate_id="candidate-final",
        base_map_seq=1,
    )
    workflow = ModificationWorkflow(store)
    _submitted(workflow, "node-final", candidate_id=setup.candidate_id)
    executor = _Executor(
        VerificationResult(
            event=ModificationEvent.FINAL_VERIFICATION_PASSED,
            status="passed",
            summary={"required_commands_passed": 1},
        )
    )
    orchestrator = VerificationOrchestrator(store, executor)

    result = await orchestrator.reconcile_node("node-final")

    assert result["phase"] == "final"
    assert result["state"] == "completed"
    assert workflow.get("node-final")["state"] == ModificationState.READY_TO_PUBLISH.value
    assert len(executor.calls) == 1
    assert json.loads(
        (setup.workspace.root / "result.json").read_text(encoding="utf-8")
    )["status"] == "ready"


@pytest.mark.asyncio
async def test_duplicate_delivery_while_running_does_not_start_a_second_run(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    _red_allowed(workflow, "node-concurrent")
    executor = _BlockingExecutor(
        VerificationResult(
            event=ModificationEvent.RED_CONFIRMED,
            status="expected_red",
            error_code="expected_red",
        )
    )
    first = VerificationOrchestrator(store, executor)
    duplicate = VerificationOrchestrator(store, executor)

    task = asyncio.create_task(first.reconcile_node("node-concurrent"))
    await executor.started.wait()
    in_progress = await duplicate.reconcile_node("node-concurrent")
    executor.release.set()
    completed = await task

    assert in_progress["state"] == "running"
    assert completed["state"] == "completed"
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_long_verification_renews_lease_before_second_orchestrator_can_claim(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    _red_allowed(workflow, "node-renew")
    clock = _Clock()
    executor = _CountingBlockingExecutor(
        VerificationResult(
            event=ModificationEvent.RED_CONFIRMED,
            status="expected_red",
            error_code="expected_red",
        )
    )
    first = VerificationOrchestrator(store, executor, lease_seconds=1, clock=clock)
    duplicate = VerificationOrchestrator(store, executor, lease_seconds=1, clock=clock)

    task = asyncio.create_task(first.reconcile_node("node-renew"))
    await executor.started.wait()
    original_expiry = float(first.status("node-renew")["lease_expires_at"])
    clock.advance(0.5)
    for _ in range(20):
        await asyncio.sleep(0.05)
        renewed = first.status("node-renew")
        if float(renewed["lease_expires_at"]) > original_expiry:
            break
    assert float(first.status("node-renew")["lease_expires_at"]) > original_expiry

    clock.advance(0.75)
    in_progress = await duplicate.reconcile_node("node-renew")
    executor.release.set()
    completed = await task

    assert in_progress["state"] == "running"
    assert completed["state"] == "completed"
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_expired_running_lease_is_recovered_after_process_restart(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    _red_allowed(workflow, "node-restart")
    clock = _Clock()
    crashed = VerificationOrchestrator(
        store,
        _Executor(SystemExit("simulated_process_crash")),
        lease_seconds=10,
        clock=clock,
    )

    with pytest.raises(SystemExit, match="simulated_process_crash"):
        await crashed.reconcile_node("node-restart")
    assert crashed.status("node-restart")["state"] == "running"

    clock.advance(11)
    reopened = ProjectPlanStore.open_existing(store.project_root)
    recovered_executor = _Executor(
        VerificationResult(
            event=ModificationEvent.RED_CONFIRMED,
            status="expected_red",
            error_code="expected_red",
        )
    )
    recovered = VerificationOrchestrator(
        reopened,
        recovered_executor,
        lease_seconds=10,
        clock=clock,
    )

    results = await recovered.recover()

    assert results[0]["state"] == "completed"
    assert results[0]["attempt"] == 2
    assert ModificationWorkflow(reopened).get("node-restart")["state"] == (
        ModificationState.RED_CONFIRMED.value
    )
    assert len(recovered_executor.calls) == 1


@pytest.mark.asyncio
async def test_temporary_container_unavailability_is_deferred_then_retried(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    _red_allowed(workflow, "node-retry")
    executor = _Executor(
        TemporaryVerificationUnavailable("container_temporarily_unavailable"),
        VerificationResult(
            event=ModificationEvent.RED_CONFIRMED,
            status="expected_red",
            error_code="expected_red",
        ),
    )
    clock = _Clock()
    orchestrator = VerificationOrchestrator(store, executor, clock=clock)

    deferred = await orchestrator.reconcile_node("node-retry")
    clock.advance(1)
    completed = await orchestrator.reconcile_node("node-retry")

    assert deferred["state"] == "queued"
    assert deferred["error_code"] == "container_temporarily_unavailable"
    assert workflow.get("node-retry")["state"] == ModificationState.RED_CONFIRMED.value
    assert completed["state"] == "completed"
    assert completed["attempt"] == 2
    assert len(executor.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["red", "final"])
async def test_temporary_failure_uses_persisted_bounded_backoff_and_stable_exhaustion(
    store: ProjectPlanStore,
    phase: str,
) -> None:
    workflow = ModificationWorkflow(store)
    node_id = f"node-bounded-retry-{phase}"
    if phase == "red":
        _red_allowed(workflow, node_id)
    else:
        _submitted(workflow, node_id)
    clock = _Clock()
    executor = _Executor(
        *(
            TemporaryVerificationUnavailable("container_temporarily_unavailable")
            for _ in range(5)
        )
    )
    orchestrator = VerificationOrchestrator(store, executor, clock=clock)

    for attempt, delay in enumerate((1, 2, 4, 8), start=1):
        deferred = await orchestrator.reconcile_node(node_id)
        assert deferred["state"] == "queued"
        assert deferred["attempt"] == attempt
        assert deferred["next_retry_at"] == clock.current.timestamp() + delay
        assert deferred["max_attempts"] == 5

        not_due = await orchestrator.reconcile_node(node_id)
        assert not_due == deferred
        assert len(executor.calls) == attempt
        clock.advance(delay)

    exhausted = await orchestrator.reconcile_node(node_id)
    duplicate = await orchestrator.reconcile_node(node_id)

    assert exhausted["state"] == "failed"
    assert exhausted["attempt"] == 5
    assert exhausted["next_retry_at"] is None
    assert exhausted["terminal_reason"] == "verification_retry_exhausted"
    assert duplicate == exhausted
    assert len(executor.calls) == 5
    assert workflow.get(node_id)["state"] == ModificationState.VERIFICATION_BLOCKED.value
    exhausted_events = [
        event
        for event in workflow.events(node_id)
        if event["event"] == ModificationEvent.VERIFICATION_RETRY_EXHAUSTED.value
    ]
    assert len(exhausted_events) == 1
    assert exhausted_events[0]["payload"]["phase"] == phase
    assert exhausted_events[0]["payload"]["terminal_reason"] == (
        "verification_retry_exhausted"
    )

    reopened = ProjectPlanStore.open_existing(store.project_root)
    after_restart_executor = _Executor()
    after_restart = VerificationOrchestrator(
        reopened,
        after_restart_executor,
        clock=clock,
    )

    assert await after_restart.recover() == []
    assert await after_restart.reconcile_node(node_id) == exhausted
    assert after_restart_executor.calls == []


@pytest.mark.asyncio
async def test_authoritative_summary_and_logs_enforce_minimal_evidence_boundary(
    store: ProjectPlanStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = store.project_root / "src" / "minimal.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file = store.project_root / "tests" / "test_minimal.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_minimal():\n    assert True\n", encoding="utf-8")
    command = "python -m pytest tests/test_minimal.py -q"
    setup = CandidateExecutionService(store.project_root).prepare_from_snapshot(
        {
            "module_id": "module-minimal-evidence",
            "node_id": "node-minimal-evidence",
            "implementation_entities": [
                {"entity_id": "impl-minimal", "path": "src/minimal.py"}
            ],
            "test_entities": [
                {"entity_id": "test-minimal", "path": "tests/test_minimal.py"}
            ],
            "test_commands": [command],
            "interfaces": [],
            "test_dir": "tests",
        },
        run_id="setup-minimal-evidence",
        candidate_id="candidate-minimal-evidence",
        base_map_seq=1,
    )
    workflow = ModificationWorkflow(store)
    _submitted(
        workflow,
        "node-minimal-evidence",
        candidate_id=setup.candidate_id,
    )
    executor = _Executor(
        VerificationResult(
            event=ModificationEvent.FINAL_VERIFICATION_PASSED,
            status="passed",
            summary={
                "executed_command_ids": ["command-minimal"],
                "stdout": "o" * 5000,
                "stderr": "e" * 5000,
                "source": "source-secret",
                "patch": "patch-secret",
                "diff": "diff-secret",
                "raw_command": "python secret-command",
            },
        )
    )
    orchestrator = VerificationOrchestrator(store, executor)

    with caplog.at_level(logging.INFO, logger="bridle"):
        completed = await orchestrator.reconcile_node("node-minimal-evidence")

    outcome = completed["outcome"]
    summary = outcome["summary"]
    assert summary["executed_command_ids"] == ["command-minimal"]
    assert summary["stdout_preview"] == "o" * 2048
    assert summary["stderr_preview"] == "e" * 2048
    assert {"source", "patch", "diff", "stdout", "stderr"}.isdisjoint(summary)
    assert isinstance(outcome["duration_ms"], int)
    assert outcome["duration_ms"] >= 0

    result_path = setup.workspace.root / "result.json"
    assert list(setup.workspace.root.glob("result*.json")) == [result_path]
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert result_payload["candidate_id"] == setup.candidate_id
    assert result_payload["status"] == "ready"
    assert result_payload["error_code"] is None
    assert result_payload["patches"] == []
    assert result_payload["test_results"] == []
    assert result_payload["container"] == {}
    verification = result_payload["verification"]
    assert set(verification) == {
        "run_id",
        "node_id",
        "phase",
        "source_revision",
        "contract_version",
        "candidate_id",
        "attempt",
        "status",
        "duration_ms",
        "error_code",
        "summary",
    }
    assert verification["run_id"] == completed["run_id"]
    assert verification["node_id"] == "node-minimal-evidence"
    assert verification["candidate_id"] == setup.candidate_id
    assert verification["attempt"] == 1
    assert verification["status"] == "passed"
    assert verification["duration_ms"] == outcome["duration_ms"]
    assert verification["error_code"] is None
    assert verification["summary"] == summary
    persisted_run = store.get_verification_run(str(completed["run_id"]))
    assert persisted_run is not None
    assert persisted_run["outcome"] == outcome

    details = [
        record.detail
        for record in caplog.records
        if isinstance(getattr(record, "detail", None), dict)
    ]
    assert details
    assert any("duration_ms" in detail for detail in details)
    persisted_log = next(
        record
        for record in caplog.records
        if getattr(record, "action", None) == "candidate_result_persisted"
    )
    assert persisted_log.status == "completed"
    assert persisted_log.detail == {
        "run_id": completed["run_id"],
        "node_id": "node-minimal-evidence",
        "candidate_id": setup.candidate_id,
        "attempt": 1,
        "status": "ready",
        "duration_ms": outcome["duration_ms"],
        "error_code": None,
    }
    serialized = json.dumps(
        {
            "outcome": outcome,
            "result": result_payload,
            "details": details,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for forbidden in (
        "source-secret",
        "patch-secret",
        "diff-secret",
        "nested-source-secret",
        "nested-diff-secret",
        "secret-command",
        "\"source\"",
        "\"patch\"",
        "\"diff\"",
    ):
        assert forbidden not in serialized
    assert "o" * 2049 not in serialized
    assert "e" * 2049 not in serialized


@pytest.mark.asyncio
async def test_candidate_executor_expires_mismatched_frozen_identity(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    _event(workflow, "node-stale-candidate", ModificationEvent.START, 1)
    contract = freeze_test_contract_for_workflow(
        workflow,
        "node-stale-candidate",
        1,
    )
    source = store.project_root / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file = store.project_root / "tests" / "test_module.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_value():\n    assert True\n", encoding="utf-8")
    setup = CandidateExecutionService(store.project_root).prepare_from_snapshot(
        {
            "module_id": "mod-stale-candidate",
            "node_id": "node-stale-candidate",
            "implementation_entities": [
                {"entity_id": "entity-module", "path": "src/module.py"}
            ],
            "test_entities": [
                {"entity_id": "entity-test-module", "path": "tests/test_module.py"}
            ],
            "test_commands": ["python -m pytest tests/test_module.py -q"],
            "interfaces": [],
            "test_dir": "tests",
        },
        run_id="run-stale-candidate",
        candidate_id="cand-stale-candidate",
        base_map_seq=contract.map_seq + 1,
    )

    result = await CandidateVerificationExecutor(store).execute(
        run={
            "run_id": "verify-stale-candidate",
            "node_id": "node-stale-candidate",
            "candidate_id": setup.request.candidate_id,
            "phase": "final",
        },
        contract=contract,
    )

    assert result.event == ModificationEvent.BASELINE_EXPIRED
    assert result.status == "baseline_expired"
    assert result.error_code == "verification_candidate_contract_mismatch"
    assert result.summary == {}


def test_status_is_read_only_and_returns_minimal_authoritative_result(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    _red_allowed(workflow, "node-status")
    orchestrator = VerificationOrchestrator(store, _Executor())

    assert orchestrator.status("node-status") is None
    assert workflow.get("node-status")["state"] == ModificationState.RED_ALLOWED.value


def test_existing_schema_five_database_is_migrated_in_place(tmp_path) -> None:
    original = ProjectPlanStore(tmp_path, project_id="project-migrate")
    original.initialize(scan_if_created=False)
    with closing(sqlite3.connect(original.database_path)) as connection:
        connection.execute("DROP TABLE verification_runs")
        connection.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")
        connection.commit()

    reopened = ProjectPlanStore.open_existing(tmp_path)

    with closing(sqlite3.connect(reopened.database_path)) as connection:
        schema_version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'verification_runs'"
        ).fetchone()
    assert schema_version == ("8",)
    assert table == ("verification_runs",)


def test_stale_lease_cannot_overwrite_the_only_authoritative_result(
    store: ProjectPlanStore,
) -> None:
    workflow = ModificationWorkflow(store)
    contract_version = _red_allowed(workflow, "node-stale-lease")
    run = store.enqueue_verification_run(
        run_id="verify-stale-lease",
        node_id="node-stale-lease",
        phase="red",
        source_revision=3,
        contract_version=contract_version,
    )
    first = store.claim_verification_run(
        run["run_id"],
        now_timestamp=10.0,
        lease_seconds=5,
    )
    second = store.claim_verification_run(
        run["run_id"],
        now_timestamp=16.0,
        lease_seconds=5,
    )
    assert first is not None
    assert second is not None
    accepted = store.complete_verification_run(
        run["run_id"],
        lease_token=second["lease_token"],
        outcome={"event": "red_confirmed", "status": "expected_red", "summary": {}},
        error_code="expected_red",
    )

    stale = store.complete_verification_run(
        run["run_id"],
        lease_token=first["lease_token"],
        outcome={"event": "invalid_test", "status": "invalid", "summary": {}},
        error_code="invalid_test",
    )

    assert stale == accepted
    assert stale["outcome"]["event"] == "red_confirmed"
