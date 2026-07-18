"""Persistent state machine for project modification and verification gates."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from bridle.api.errors import NotFoundError


class ModificationState(StrEnum):
    TEST_AUTHORING = "TEST_AUTHORING"
    TEST_CONTRACT_APPROVED = "TEST_CONTRACT_APPROVED"
    RED_ALLOWED = "RED_ALLOWED"
    RED_VERIFYING = "RED_VERIFYING"
    RED_CONFIRMED = "RED_CONFIRMED"
    IMPLEMENTING = "IMPLEMENTING"
    SUBMITTED = "SUBMITTED"
    FINAL_VERIFYING = "FINAL_VERIFYING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED = "PUBLISHED"
    VERIFICATION_BLOCKED = "VERIFICATION_BLOCKED"


class ModificationEvent(StrEnum):
    START = "start"
    TEST_CONTRACT_APPROVED = "test_contract_approved"
    RED_ALLOWED = "red_allowed"
    RED_VERIFICATION_STARTED = "red_verification_started"
    RED_CONFIRMED = "red_confirmed"
    IMPLEMENTATION_STARTED = "implementation_started"
    SUBMITTED = "submitted"
    FINAL_VERIFICATION_STARTED = "final_verification_started"
    FINAL_VERIFICATION_PASSED = "final_verification_passed"
    PUBLISHED = "published"
    INVALID_TEST = "invalid_test"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    BASELINE_EXPIRED = "baseline_expired"
    FINAL_VERIFICATION_FAILED = "final_verification_failed"
    VERIFICATION_RETRY_EXHAUSTED = "verification_retry_exhausted"


class ModificationStore(Protocol):
    def get_modification_workflow(self, node_id: str) -> dict[str, Any] | None: ...

    def list_modification_events(self, node_id: str) -> list[dict[str, Any]]: ...

    def apply_modification_transition(
        self,
        node_id: str,
        *,
        event: str,
        event_id: str,
        allowed_from: set[str | None],
        to_state: str,
        expected_revision: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_active_test_contract(self, node_id: str) -> dict[str, Any] | None: ...

    def freeze_test_contract(
        self,
        node_id: str,
        *,
        contract_version: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]: ...

    def invalidate_test_contract(
        self,
        node_id: str,
        *,
        contract_version: str,
        reason: str,
    ) -> dict[str, Any]: ...


_S = ModificationState
_E = ModificationEvent
_TRANSITIONS: dict[ModificationEvent, tuple[set[ModificationState | None], ModificationState]] = {
    _E.START: ({None}, _S.TEST_AUTHORING),
    _E.TEST_CONTRACT_APPROVED: ({_S.TEST_AUTHORING}, _S.TEST_CONTRACT_APPROVED),
    _E.RED_ALLOWED: ({_S.TEST_CONTRACT_APPROVED}, _S.RED_ALLOWED),
    _E.RED_VERIFICATION_STARTED: ({_S.RED_ALLOWED}, _S.RED_VERIFYING),
    _E.RED_CONFIRMED: ({_S.RED_VERIFYING}, _S.RED_CONFIRMED),
    _E.IMPLEMENTATION_STARTED: ({_S.RED_CONFIRMED}, _S.IMPLEMENTING),
    _E.SUBMITTED: ({_S.IMPLEMENTING}, _S.SUBMITTED),
    _E.FINAL_VERIFICATION_STARTED: ({_S.SUBMITTED}, _S.FINAL_VERIFYING),
    _E.FINAL_VERIFICATION_PASSED: ({_S.FINAL_VERIFYING}, _S.READY_TO_PUBLISH),
    _E.PUBLISHED: ({_S.READY_TO_PUBLISH}, _S.PUBLISHED),
    _E.INVALID_TEST: ({_S.RED_VERIFYING}, _S.TEST_AUTHORING),
    _E.INFRASTRUCTURE_FAILED: ({_S.RED_VERIFYING}, _S.RED_ALLOWED),
    _E.BASELINE_EXPIRED: (
        {
            _S.TEST_CONTRACT_APPROVED,
            _S.RED_ALLOWED,
            _S.RED_VERIFYING,
            _S.RED_CONFIRMED,
            _S.IMPLEMENTING,
            _S.SUBMITTED,
            _S.FINAL_VERIFYING,
            _S.READY_TO_PUBLISH,
        },
        _S.TEST_AUTHORING,
    ),
    _E.FINAL_VERIFICATION_FAILED: ({_S.FINAL_VERIFYING}, _S.IMPLEMENTING),
    _E.VERIFICATION_RETRY_EXHAUSTED: (
        {_S.RED_VERIFYING, _S.FINAL_VERIFYING},
        _S.VERIFICATION_BLOCKED,
    ),
}


class ModificationWorkflow:
    """Validate transitions while the project-local store owns persistence."""

    def __init__(self, store: ModificationStore) -> None:
        self._store = store

    def current(self, node_id: str) -> dict[str, Any] | None:
        return self._store.get_modification_workflow(node_id)

    def get(self, node_id: str) -> dict[str, Any]:
        result = self.current(node_id)
        if result is None:
            raise NotFoundError(
                resource="modification_workflow",
                message="Modification workflow not found",
                details={"node_id": node_id},
            )
        return result

    def events(self, node_id: str) -> list[dict[str, Any]]:
        return self._store.list_modification_events(node_id)

    def active_test_contract(self, node_id: str) -> dict[str, Any] | None:
        return self._store.get_active_test_contract(node_id)

    def freeze_test_contract(
        self,
        node_id: str,
        *,
        contract_version: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return self._store.freeze_test_contract(
            node_id,
            contract_version=contract_version,
            snapshot=snapshot,
        )

    def invalidate_test_contract(
        self,
        node_id: str,
        *,
        contract_version: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._store.invalidate_test_contract(
            node_id,
            contract_version=contract_version,
            reason=reason,
        )

    def apply(
        self,
        node_id: str,
        *,
        event: ModificationEvent,
        event_id: str,
        expected_revision: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed, target = _TRANSITIONS[event]
        return self._store.apply_modification_transition(
            node_id,
            event=event.value,
            event_id=event_id,
            allowed_from={None if state is None else state.value for state in allowed},
            to_state=target.value,
            expected_revision=expected_revision,
            payload=payload,
        )
