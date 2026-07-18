"""Durable state-driven orchestration for authoritative test verification."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from bridle.agent.container.candidate_contract import (
    CandidateExecutionRequest,
    CandidateExecutionResult,
    FrozenTestContract,
    compute_patches,
    persist_result,
    snapshot_directory_hashes,
)
from bridle.agent.runtime.modification_workflow import (
    ModificationEvent,
    ModificationState,
    ModificationWorkflow,
)
from bridle.api.errors import ConflictError
from bridle.features.project_map.store import ProjectPlanStore

logger = logging.getLogger("bridle")

_WORKFLOW_FAILURE_MATRIX: dict[str, dict[str, Any]] = {
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


def classify_workflow_failure(error_code: str) -> dict[str, Any]:
    """Return the single retry, recovery, outcome, and evidence policy for a failure."""
    try:
        return dict(_WORKFLOW_FAILURE_MATRIX[error_code])
    except KeyError as exc:
        raise ValueError(f"unknown_workflow_failure:{error_code}") from exc


@dataclass(frozen=True)
class VerificationResult:
    """Minimal authoritative result accepted by the modification gate."""

    event: ModificationEvent
    status: str
    error_code: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


class VerificationExecutor(Protocol):
    async def execute(
        self,
        *,
        run: dict[str, Any],
        contract: FrozenTestContract,
    ) -> VerificationResult: ...


class TemporaryVerificationUnavailable(RuntimeError):
    """A retryable infrastructure failure that must not produce a gate result."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _load_candidate_request(
    store: ProjectPlanStore,
    *,
    candidate_id: str,
    node_id: str,
) -> CandidateExecutionRequest:
    if not candidate_id:
        raise TemporaryVerificationUnavailable("verification_candidate_missing")
    manifests = list(
        (
            store.project_root
            / ".bridle"
            / "runtime"
            / "modules"
        ).glob(f"*/candidates/{candidate_id}/workspace-manifest.json")
    )
    if len(manifests) != 1:
        raise TemporaryVerificationUnavailable("verification_candidate_manifest_missing")
    try:
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        request = CandidateExecutionRequest.from_dict(manifest["candidate_request"])
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemporaryVerificationUnavailable(
            "verification_candidate_manifest_invalid"
        ) from exc
    if request.candidate_id != candidate_id or request.node_id != node_id:
        raise TemporaryVerificationUnavailable("verification_candidate_identity_mismatch")
    return request


class CandidateVerificationExecutor:
    """Rebuild the existing candidate test backend from its durable workspace manifest."""

    def __init__(self, store: ProjectPlanStore) -> None:
        self._store = store

    async def execute(
        self,
        *,
        run: dict[str, Any],
        contract: FrozenTestContract,
    ) -> VerificationResult:
        from bridle.agent.container.container_service import get_shared_container_backend
        from bridle.agent.container.test_backend import ModuleContainerTestBackend
        from bridle.agent.safety.sandbox_policy import SandboxPolicy

        candidate_id = str(run.get("candidate_id") or "")
        request = _load_candidate_request(
            self._store,
            candidate_id=candidate_id,
            node_id=str(run["node_id"]),
        )
        if (
            request.base_map_seq != contract.map_seq
            or request.boundary_fingerprint != contract.boundary_fingerprint
            or request.image_version != contract.image_version
        ):
            return VerificationResult(
                event=ModificationEvent.BASELINE_EXPIRED,
                status="baseline_expired",
                error_code="verification_candidate_contract_mismatch",
            )

        commands = [command.raw_command for command in contract.commands]
        command_ids = [command.command_id for command in contract.commands]
        policy = SandboxPolicy.for_run(
            run_id=str(run["run_id"]),
            node_id=str(run["node_id"]),
            workspace_root=request.project_dir,
            allowed_files=sorted(set(request.write_set) | set(request.read_set)),
            node_tests=commands,
            command_timeout_seconds=request.timeout_seconds,
            network_allowed=request.network_allowed,
        )
        backend = ModuleContainerTestBackend(
            get_shared_container_backend(self._store.project_root),
            candidate_request=request,
            candidate_root=str(request.candidate_root),
            module_root=str(request.candidate_root.parent.parent),
            candidate_rel=f"candidates/{request.candidate_id}",
            test_entity_id=str(run["node_id"]),
            required_commands=commands,
            required_command_ids=command_ids,
            map_seq=contract.map_seq,
            test_contract=contract,
            red_verification=run["phase"] == "red",
        )
        payload = await backend.run_authoritative_tests(policy=policy)
        if payload.get("retryable") is True:
            raise TemporaryVerificationUnavailable(
                str(payload.get("error_code") or "verification_container_unavailable")
            )
        evidence = backend.collect_evidence()
        summary = {
            "executed_command_ids": list(evidence.executed_command_ids),
            "failed_command_ids": list(evidence.failed_command_ids),
        }
        if run["phase"] == "red":
            classification = dict(payload.get("red_classification") or {})
            event = {
                "EXPECTED_RED": ModificationEvent.RED_CONFIRMED,
                "UNEXPECTED_RED": ModificationEvent.INVALID_TEST,
                "INVALID_TEST": ModificationEvent.INVALID_TEST,
                "INFRA_ERROR": ModificationEvent.INFRASTRUCTURE_FAILED,
                "BASELINE_REGRESSION": ModificationEvent.BASELINE_EXPIRED,
            }.get(str(classification.get("classification")))
            if event is None:
                raise TemporaryVerificationUnavailable("red_classification_missing")
            summary.update(
                {
                    "classification": classification.get("classification"),
                    "failed_case_ids": list(classification.get("failed_case_ids") or []),
                    "unexpected_case_ids": list(
                        classification.get("unexpected_case_ids") or []
                    ),
                    "baseline_failed_case_ids": list(
                        classification.get("baseline_failed_case_ids") or []
                    ),
                }
            )
            return VerificationResult(
                event=event,
                status=str(classification.get("classification") or "invalid").lower(),
                error_code=str(classification.get("error_code") or "invalid_test"),
                summary=summary,
            )

        passed = payload.get("status") == "completed" and evidence.all_required_passed
        return VerificationResult(
            event=(
                ModificationEvent.FINAL_VERIFICATION_PASSED
                if passed
                else ModificationEvent.FINAL_VERIFICATION_FAILED
            ),
            status="passed" if passed else "failed",
            error_code=None if passed else str(payload.get("error_code") or "test_failed"),
            summary=summary,
        )


_TRIGGER = {
    ModificationState.RED_ALLOWED.value: (
        "red",
        ModificationEvent.RED_VERIFICATION_STARTED,
    ),
    ModificationState.SUBMITTED.value: (
        "final",
        ModificationEvent.FINAL_VERIFICATION_STARTED,
    ),
}
_RUNNING_PHASE = {
    ModificationState.RED_VERIFYING.value: "red",
    ModificationState.FINAL_VERIFYING.value: "final",
}
_ALLOWED_RESULT_EVENTS = {
    "red": {
        ModificationEvent.RED_CONFIRMED,
        ModificationEvent.INVALID_TEST,
        ModificationEvent.INFRASTRUCTURE_FAILED,
        ModificationEvent.BASELINE_EXPIRED,
    },
    "final": {
        ModificationEvent.FINAL_VERIFICATION_PASSED,
        ModificationEvent.FINAL_VERIFICATION_FAILED,
        ModificationEvent.BASELINE_EXPIRED,
    },
}
_RECOVERABLE_STATES = set(_TRIGGER) | set(_RUNNING_PHASE)


class VerificationOrchestrator:
    """Consume persisted workflow states and produce one authoritative result per event."""

    def __init__(
        self,
        store: ProjectPlanStore,
        executor: VerificationExecutor,
        *,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("verification_lease_seconds_invalid")
        self._store = store
        self._workflow = ModificationWorkflow(store)
        self._executor = executor
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def status(self, node_id: str) -> dict[str, Any] | None:
        """Read the latest durable run without dispatching, retrying, or advancing it."""
        return self._store.latest_verification_run(node_id)

    def next_retry_at(self) -> float | None:
        return self._store.next_verification_retry_at()

    async def recover(self) -> list[dict[str, Any]]:
        """Resume trigger states, queued runs, and lease-expired running work after restart."""
        workflows = self._store.list_modification_workflows(states=_RECOVERABLE_STATES)
        results: list[dict[str, Any]] = []
        for workflow in workflows:
            result = await self.reconcile_node(str(workflow["node_id"]))
            if result is not None:
                results.append(result)
        return results

    async def reconcile_node(self, node_id: str) -> dict[str, Any] | None:
        """Drive one node from a persisted trigger through one authoritative result."""
        workflow = self._workflow.current(node_id)
        if workflow is None:
            return self.status(node_id)

        state = str(workflow["state"])
        if state in _TRIGGER:
            run = self._enqueue_from_trigger(workflow)
            _, start_event = _TRIGGER[state]
            try:
                workflow = self._workflow.apply(
                    node_id,
                    event=start_event,
                    event_id=f"verification:{run['run_id']}:started",
                    expected_revision=int(workflow["revision"]),
                    payload={
                        "verification_run_id": run["run_id"],
                        "phase": run["phase"],
                    },
                )
            except ConflictError as exc:
                if exc.api_error.code != "modification_revision_conflict":
                    raise
                workflow = self._workflow.get(node_id)
            state = str(workflow["state"])

        phase = _RUNNING_PHASE.get(state)
        if phase is None:
            return self.status(node_id)
        run = self.status(node_id)
        if run is None or run["phase"] != phase:
            raise RuntimeError("verification_run_missing_for_active_gate")
        if run["state"] == "completed":
            self._apply_authoritative_result(run)
            return self.status(node_id)

        now = self._clock()
        claimed = self._store.claim_verification_run(
            str(run["run_id"]),
            now_timestamp=now.timestamp(),
            lease_seconds=self._lease_seconds,
        )
        if claimed is None:
            return self.status(node_id)

        logger.info(
            "verification_run_started",
            extra={
                "action": "verification_run_started",
                "status": "started",
                "detail": self._log_detail(claimed),
            },
        )
        contract_row = self._store.get_test_contract(
            node_id,
            str(claimed["contract_version"]),
        )
        if contract_row is None:
            raise RuntimeError("verification_test_contract_missing")
        contract = FrozenTestContract.from_dict(contract_row["snapshot"])
        execution_started = time.perf_counter()
        lease_stop = asyncio.Event()
        lease_renewal = asyncio.create_task(
            self._renew_lease_until_stopped(claimed, lease_stop)
        )
        temporary_error: TemporaryVerificationUnavailable | None = None
        try:
            result = await self._executor.execute(run=claimed, contract=contract)
        except TemporaryVerificationUnavailable as exc:
            temporary_error = exc
        except BaseException:
            await self._stop_lease_renewal(
                claimed,
                stop=lease_stop,
                task=lease_renewal,
            )
            raise

        lease_owned = await self._stop_lease_renewal(
            claimed,
            stop=lease_stop,
            task=lease_renewal,
        )
        if not lease_owned:
            return self.status(node_id)

        duration_ms = max(0, int((time.perf_counter() - execution_started) * 1000))
        if temporary_error is not None:
            deferred = self._store.defer_verification_run(
                str(claimed["run_id"]),
                lease_token=str(claimed["lease_token"]),
                error_code=temporary_error.error_code,
                now_timestamp=self._clock().timestamp(),
            )
            exhausted = deferred["state"] == "failed"
            logger.warning(
                (
                    "verification_run_retry_exhausted"
                    if exhausted
                    else "verification_run_deferred"
                ),
                extra={
                    "action": (
                        "verification_run_retry_exhausted"
                        if exhausted
                        else "verification_run_deferred"
                    ),
                    "status": "blocked" if exhausted else "retry",
                    "error_code": temporary_error.error_code,
                    "detail": self._log_detail(deferred, duration_ms=duration_ms),
                },
            )
            return deferred

        self._validate_result(phase, result)
        completed = self._store.complete_verification_run(
            str(claimed["run_id"]),
            lease_token=str(claimed["lease_token"]),
            outcome={
                "event": result.event.value,
                "status": result.status,
                "error_code": result.error_code,
                "summary": self._sanitize_summary(result.summary),
                "duration_ms": duration_ms,
            },
            error_code=result.error_code,
        )
        self._apply_authoritative_result(completed)
        logger.info(
            "verification_run_finished",
            extra={
                "action": "verification_run_finished",
                "status": "completed",
                "detail": self._log_detail(completed, duration_ms=duration_ms),
            },
        )
        return self.status(node_id)

    async def _renew_lease_until_stopped(
        self,
        run: dict[str, Any],
        stop: asyncio.Event,
    ) -> None:
        interval_seconds = max(0.1, self._lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                self._store.renew_verification_lease(
                    str(run["run_id"]),
                    lease_token=str(run["lease_token"]),
                    now_timestamp=self._clock().timestamp(),
                    lease_seconds=self._lease_seconds,
                )
                continue
            return

    async def _stop_lease_renewal(
        self,
        run: dict[str, Any],
        *,
        stop: asyncio.Event,
        task: asyncio.Task[None],
    ) -> bool:
        stop.set()
        try:
            await task
        except ConflictError as exc:
            logger.warning(
                "verification_lease_lost",
                extra={
                    "action": "verification_lease_lost",
                    "status": "discarded",
                    "error_code": exc.api_error.code,
                    "detail": self._log_detail(run),
                },
            )
            return False
        return True

    def _enqueue_from_trigger(self, workflow: dict[str, Any]) -> dict[str, Any]:
        node_id = str(workflow["node_id"])
        phase, _ = _TRIGGER[str(workflow["state"])]
        contract_version = workflow.get("test_contract_version")
        if not contract_version:
            raise RuntimeError("verification_test_contract_required")
        source_revision = int(workflow["revision"])
        identity = f"{node_id}:{phase}:{source_revision}:{contract_version}"
        run_id = f"verify-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
        return self._store.enqueue_verification_run(
            run_id=run_id,
            node_id=node_id,
            phase=phase,
            source_revision=source_revision,
            contract_version=str(contract_version),
            candidate_id=self._candidate_id_from_events(node_id),
        )

    def _candidate_id_from_events(self, node_id: str) -> str | None:
        for event in reversed(self._workflow.events(node_id)):
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("candidate_id"):
                return str(payload["candidate_id"])
        return None

    def _apply_authoritative_result(self, run: dict[str, Any]) -> None:
        outcome = run.get("outcome")
        if not isinstance(outcome, dict):
            raise RuntimeError("verification_authoritative_outcome_missing")
        event = ModificationEvent(str(outcome["event"]))
        self._validate_result(
            str(run["phase"]),
            VerificationResult(
                event=event,
                status=str(outcome.get("status") or ""),
                error_code=(
                    None if outcome.get("error_code") is None else str(outcome["error_code"])
                ),
                summary=dict(outcome.get("summary") or {}),
            ),
        )
        self._persist_candidate_result(run, outcome)
        self._workflow.apply(
            str(run["node_id"]),
            event=event,
            event_id=f"verification:{run['run_id']}:result",
            payload={
                "verification_run_id": run["run_id"],
                "phase": run["phase"],
                "status": outcome.get("status"),
                "error_code": outcome.get("error_code"),
                "summary": dict(outcome.get("summary") or {}),
            },
        )

    def _persist_candidate_result(
        self,
        run: dict[str, Any],
        outcome: dict[str, Any],
    ) -> None:
        if run["phase"] != "final":
            return
        candidate_id = str(run.get("candidate_id") or "")
        request = _load_candidate_request(
            self._store,
            candidate_id=candidate_id,
            node_id=str(run["node_id"]),
        )
        write_set = list(request.write_set)
        base_hashes = snapshot_directory_hashes(
            request.candidate_root / "baseline",
            write_set,
        )
        candidate_hashes = snapshot_directory_hashes(
            request.project_dir,
            write_set,
        )
        changed_paths, _ = compute_patches(
            base_hashes=base_hashes,
            candidate_hashes=candidate_hashes,
            write_set=write_set,
        )
        event = ModificationEvent(str(outcome["event"]))
        status = (
            "ready"
            if event is ModificationEvent.FINAL_VERIFICATION_PASSED
            else "blocked"
        )
        duration_ms = max(0, int(outcome.get("duration_ms") or 0))
        error_code = (
            None
            if outcome.get("error_code") is None
            else str(outcome["error_code"])
        )
        verification = {
            "run_id": str(run["run_id"]),
            "node_id": str(run["node_id"]),
            "phase": str(run["phase"]),
            "source_revision": int(run["source_revision"]),
            "contract_version": str(run["contract_version"]),
            "candidate_id": candidate_id,
            "attempt": int(run["attempt"]),
            "status": str(outcome.get("status") or ""),
            "duration_ms": duration_ms,
            "error_code": error_code,
            "summary": dict(outcome.get("summary") or {}),
        }
        persist_result(
            CandidateExecutionResult(
                status=status,
                changed_paths=tuple(changed_paths),
                patches=(),
                base_hashes=base_hashes,
                candidate_hashes=candidate_hashes,
                test_results=(),
                container={},
                diagnostic_path=str(request.candidate_root / "diagnostics"),
                error_code=error_code,
                candidate_id=candidate_id,
                base_map_seq=request.base_map_seq,
                verification=verification,
            ),
            request.candidate_root,
        )
        logger.info(
            "candidate_result_persisted",
            extra={
                "action": "candidate_result_persisted",
                "status": "completed",
                "error_code": error_code,
                "detail": {
                    "run_id": str(run["run_id"]),
                    "node_id": str(run["node_id"]),
                    "candidate_id": candidate_id,
                    "attempt": int(run["attempt"]),
                    "status": status,
                    "duration_ms": duration_ms,
                    "error_code": error_code,
                },
            },
        )

    @staticmethod
    def _validate_result(phase: str, result: VerificationResult) -> None:
        if result.event not in _ALLOWED_RESULT_EVENTS[phase]:
            raise ValueError("verification_result_event_invalid")
        if not result.status.strip():
            raise ValueError("verification_result_status_required")

    @staticmethod
    def _sanitize_summary(summary: dict[str, Any]) -> dict[str, Any]:
        identifier_lists = {
            "failed_case_ids",
            "unexpected_case_ids",
            "baseline_failed_case_ids",
            "passed_case_ids",
            "executed_command_ids",
            "failed_command_ids",
            "required_command_ids",
        }
        text_fields = {
            "classification",
            "error_code",
        }
        sanitized: dict[str, Any] = {}
        for key in identifier_lists:
            value = summary.get(key)
            if isinstance(value, (list, tuple)):
                sanitized[key] = [
                    item[:256]
                    for item in value[:512]
                    if isinstance(item, str)
                ]
        for key in text_fields:
            value = summary.get(key)
            if isinstance(value, str):
                sanitized[key] = value[:256]
        required_passed = summary.get("required_commands_passed")
        if isinstance(required_passed, (int, float, bool)):
            sanitized["required_commands_passed"] = required_passed
        for stream in ("stdout", "stderr"):
            value = summary.get(stream, summary.get(f"{stream}_preview"))
            if isinstance(value, str):
                sanitized[f"{stream}_preview"] = value[:2048]
        return sanitized

    @staticmethod
    def _log_detail(
        run: dict[str, Any],
        *,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        detail = {
            "run_id": run["run_id"],
            "node_id": run["node_id"],
            "phase": run["phase"],
            "source_revision": run["source_revision"],
            "contract_version": run["contract_version"],
            "candidate_id": run["candidate_id"],
            "state": run["state"],
            "attempt": run["attempt"],
            "next_retry_at": run["next_retry_at"],
            "max_attempts": run["max_attempts"],
            "terminal_reason": run["terminal_reason"],
            "error_code": run["error_code"],
        }
        if duration_ms is not None:
            detail["duration_ms"] = duration_ms
        return detail
