"""Entrypoint control envelope for host-side test result persistence."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridle.agent.container.json_strict import JsonIntError, require_json_int

CONTROL_ENVELOPE_PREFIX = "BRIDLE_CONTAINER_CONTROL:"
CONTROL_ENVELOPE_SCHEMA = "bridle.container_control_envelope/v1"
CONTROL_ENVELOPE_VERSION = 1
CONTROL_EVIDENCE_SCHEMA = "bridle.container_control_evidence/v1"
HOST_ATTESTATION_SCHEMA = "bridle.container_host_attestation/v1"
RESULT_SCHEMA = "bridle.container_test_result/v1"
ENTRYPOINT_PRODUCER = "bridle.entrypoint/v1"

EXECUTION_NOT_STARTED = "not_started"
EXECUTION_STARTED = "started"
EXECUTION_EXITED = "exited"
EXECUTION_TIMED_OUT = "timed_out"
EXECUTION_FAILED_BEFORE_EXEC = "failed_before_exec"
EXECUTION_STARTED_UNKNOWN = "started_unknown"

EXECUTION_PHASE_PREPARE = "prepare"
EXECUTION_PHASE_CREATE = "create"
EXECUTION_PHASE_START = "start"
EXECUTION_PHASE_EXEC = "exec"
EXECUTION_PHASE_COLLECT = "collect"
EXECUTION_PHASE_CLEANUP = "cleanup"
EXECUTION_PHASE_FINALIZE = "finalize"

SECONDARY_COLLECT_ERROR_CODE = "active_slot_collect_failed"
SECONDARY_START_CLEANUP_ERROR_CODE = "container_start_cleanup_failed"

RUN_EVIDENCE_STATUS_PENDING = "pending"
RUN_EVIDENCE_STATUS_COMPLETED = "completed"
RUN_EVIDENCE_STATUS_FAILED = "failed"

AUTHORITATIVE_EVIDENCE_NAME = "control-envelope.json"
MANIFEST_MIRROR_NAME = "manifest.json"
MIRROR_BINDING_RUN_ID = "evidence_run_id"
MIRROR_BINDING_STATUS = "evidence_status"
AUTHORITATIVE_MIRROR_DIGEST = "mirror_digest"
_MIRROR_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ControlEnvelopeError(ValueError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


@dataclass(frozen=True)
class HostAttestationContext:
    container_id: str
    node_id: str
    test_entity_id: str
    image_digest: str
    exec_exit_code: int | None
    execution_state: str = EXECUTION_EXITED


def _require_envelope_int(
    value: object,
    *,
    field: str,
    error_code: str = "control_envelope_version_mismatch",
) -> int:
    try:
        return require_json_int(value, field=field)
    except JsonIntError as exc:
        raise ControlEnvelopeError(error_code, detail=exc.detail) from exc


def build_control_envelope(
    *,
    manifest: dict[str, Any],
    run_id: str,
    candidate_rel: str | None,
    exit_code: int,
) -> dict[str, Any]:
    return {
        "schema": CONTROL_ENVELOPE_SCHEMA,
        "version": CONTROL_ENVELOPE_VERSION,
        "producer": ENTRYPOINT_PRODUCER,
        "run_id": run_id,
        "candidate_rel": candidate_rel,
        "exit_code": int(exit_code),
        "manifest": manifest,
    }


def build_host_attestation(context: HostAttestationContext) -> dict[str, Any]:
    attestation: dict[str, Any] = {
        "schema": HOST_ATTESTATION_SCHEMA,
        "container_id": context.container_id,
        "node_id": context.node_id,
        "test_entity_id": context.test_entity_id,
        "image_digest": context.image_digest,
        "execution_state": context.execution_state,
        "exec_exit_code": context.exec_exit_code,
    }
    return attestation


def format_control_envelope_line(envelope: dict[str, Any]) -> str:
    return CONTROL_ENVELOPE_PREFIX + json.dumps(
        envelope,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _authoritative_path(candidate_root: Path) -> Path:
    return candidate_root.resolve() / "diagnostics" / AUTHORITATIVE_EVIDENCE_NAME


def _manifest_mirror_path(candidate_root: Path) -> Path:
    return candidate_root.resolve() / "output" / MANIFEST_MIRROR_NAME


def compute_mirror_digest(mirror: dict[str, Any]) -> str:
    payload = json.dumps(mirror, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bind_manifest_mirror(
    manifest_mirror: dict[str, Any],
    *,
    run_id: str,
    status: str,
) -> dict[str, Any]:
    mirror = dict(manifest_mirror)
    mirror[MIRROR_BINDING_RUN_ID] = run_id
    mirror[MIRROR_BINDING_STATUS] = status
    return mirror


def _require_authoritative_mirror_digest(authoritative: dict[str, Any]) -> str:
    raw = authoritative.get(AUTHORITATIVE_MIRROR_DIGEST)
    if raw is None or raw == "":
        raise ControlEnvelopeError("control_evidence_mirror_digest_missing")
    digest = str(raw)
    if not _MIRROR_DIGEST_PATTERN.fullmatch(digest):
        raise ControlEnvelopeError("control_evidence_mirror_digest_invalid")
    return digest


def _validate_mirror_binding(
    authoritative: dict[str, Any],
    mirror: dict[str, Any],
) -> None:
    auth_run = str(authoritative.get("run_id") or "")
    auth_status = str(authoritative.get("status") or "")
    if MIRROR_BINDING_RUN_ID not in mirror or MIRROR_BINDING_STATUS not in mirror:
        raise ControlEnvelopeError("control_evidence_mirror_binding_missing")
    mirror_run = str(mirror[MIRROR_BINDING_RUN_ID])
    mirror_status = str(mirror[MIRROR_BINDING_STATUS])
    if mirror_run != auth_run or mirror_status != auth_status:
        raise ControlEnvelopeError("control_evidence_mirror_binding_mismatch")
    expected_digest = _require_authoritative_mirror_digest(authoritative)
    actual_digest = compute_mirror_digest(mirror)
    if expected_digest != actual_digest:
        raise ControlEnvelopeError("control_evidence_mirror_digest_mismatch")


def load_authoritative_evidence(candidate_root: Path) -> dict[str, Any]:
    path = _authoritative_path(candidate_root)
    if not path.is_file():
        raise ControlEnvelopeError("control_evidence_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlEnvelopeError("control_evidence_invalid", detail=str(exc)) from exc
    if not isinstance(payload, dict):
        raise ControlEnvelopeError("control_evidence_invalid")
    return payload


def validate_evidence_manifest_sync(candidate_root: Path) -> None:
    authoritative = load_authoritative_evidence(candidate_root)
    mirror_path = _manifest_mirror_path(candidate_root)
    if not mirror_path.is_file():
        raise ControlEnvelopeError("control_evidence_mirror_missing")
    try:
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlEnvelopeError("control_evidence_mirror_invalid", detail=str(exc)) from exc
    if not isinstance(mirror, dict):
        raise ControlEnvelopeError("control_evidence_mirror_invalid")
    _validate_mirror_binding(authoritative, mirror)


def resolve_display_manifest(candidate_root: Path, *, expected_run_id: str | None = None) -> dict[str, Any]:
    authoritative = load_authoritative_evidence(candidate_root)
    if expected_run_id is not None and str(authoritative.get("run_id") or "") != expected_run_id:
        raise ControlEnvelopeError("control_evidence_run_mismatch")
    validate_evidence_manifest_sync(candidate_root)
    if authoritative.get("status") != RUN_EVIDENCE_STATUS_COMPLETED:
        raise ControlEnvelopeError("control_evidence_not_completed")
    mirror_path = _manifest_mirror_path(candidate_root)
    if not mirror_path.is_file():
        raise ControlEnvelopeError("control_evidence_mirror_missing")
    mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    if not isinstance(mirror, dict):
        raise ControlEnvelopeError("control_evidence_mirror_invalid")
    return mirror


def publish_run_evidence(
    candidate_root: Path,
    *,
    authoritative: dict[str, Any],
    manifest_mirror: dict[str, Any],
) -> None:
    candidate_root = candidate_root.resolve()
    run_id = str(authoritative.get("run_id") or "")
    status = str(authoritative.get("status") or "")
    mirror = _bind_manifest_mirror(manifest_mirror, run_id=run_id, status=status)
    mirror_digest = compute_mirror_digest(mirror)
    authoritative_payload = dict(authoritative)
    authoritative_payload[AUTHORITATIVE_MIRROR_DIGEST] = mirror_digest
    _atomic_write_json(_authoritative_path(candidate_root), authoritative_payload)
    _atomic_write_json(_manifest_mirror_path(candidate_root), mirror)


def begin_run_evidence(
    candidate_root: Path,
    *,
    run_id: str,
    candidate_rel: str,
    node_id: str,
    test_entity_id: str,
    image_digest: str,
) -> None:
    pending_evidence = {
        "schema": CONTROL_EVIDENCE_SCHEMA,
        "run_id": run_id,
        "status": RUN_EVIDENCE_STATUS_PENDING,
        "candidate_rel": candidate_rel,
        "host_attestation": {
            "schema": HOST_ATTESTATION_SCHEMA,
            "run_id": run_id,
            "node_id": node_id,
            "test_entity_id": test_entity_id,
            "image_digest": image_digest,
            "container_id": "",
            "execution_state": EXECUTION_NOT_STARTED,
            "exec_exit_code": None,
        },
    }
    pending_manifest = {
        "schema": RESULT_SCHEMA,
        "status": "pending",
        "run_id": run_id,
        "exit_code": None,
        "error_code": "run_pending",
        "results": [],
    }
    publish_run_evidence(
        candidate_root,
        authoritative=pending_evidence,
        manifest_mirror=pending_manifest,
    )


def persist_failed_run_evidence(
    candidate_root: Path,
    *,
    run_id: str,
    candidate_rel: str,
    error_code: str,
    node_id: str,
    test_entity_id: str,
    image_digest: str,
    execution_state: str,
    container_id: str | None = None,
    exec_exit_code: int | None = None,
    execution_phase: str = "",
    side_effect_possible: bool = False,
    secondary_execution_phase: str | None = None,
    secondary_error_code: str | None = None,
    secondary_detail: str | None = None,
    start_cleanup_failure: str | None = None,
    resource_may_remain: bool = False,
    secondary_diagnostics: dict[str, object] | None = None,
    detail: str = "",
) -> None:
    host_attestation: dict[str, Any] = {
        "schema": HOST_ATTESTATION_SCHEMA,
        "run_id": run_id,
        "node_id": node_id,
        "test_entity_id": test_entity_id,
        "image_digest": image_digest,
        "container_id": container_id or "",
        "execution_state": execution_state,
        "exec_exit_code": exec_exit_code,
        "execution_phase": execution_phase,
        "side_effect_possible": side_effect_possible,
    }
    if secondary_execution_phase is not None:
        host_attestation["secondary_execution_phase"] = secondary_execution_phase
    if secondary_error_code is not None:
        host_attestation["secondary_error_code"] = secondary_error_code
    if secondary_detail is not None:
        host_attestation["secondary_detail"] = secondary_detail
    if start_cleanup_failure is not None:
        host_attestation["start_cleanup_failure"] = start_cleanup_failure
    if secondary_execution_phase == EXECUTION_PHASE_CLEANUP:
        host_attestation["resource_may_remain"] = bool(resource_may_remain)
    elif resource_may_remain:
        host_attestation["resource_may_remain"] = True
    if secondary_diagnostics is not None:
        host_attestation["secondary_diagnostics"] = dict(secondary_diagnostics)
    failed_evidence = {
        "schema": CONTROL_EVIDENCE_SCHEMA,
        "run_id": run_id,
        "status": RUN_EVIDENCE_STATUS_FAILED,
        "candidate_rel": candidate_rel,
        "error_code": error_code,
        "detail": detail,
        "host_attestation": host_attestation,
    }
    failed_manifest: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": "failed",
        "run_id": run_id,
        "error_code": error_code,
        "results": [],
    }
    if exec_exit_code is not None:
        failed_manifest["exit_code"] = int(exec_exit_code)
    else:
        failed_manifest["exit_code"] = None
    publish_run_evidence(
        candidate_root,
        authoritative=failed_evidence,
        manifest_mirror=failed_manifest,
    )


def load_completed_run_evidence(candidate_root: Path, *, expected_run_id: str) -> dict[str, Any]:
    payload = load_authoritative_evidence(candidate_root)
    if str(payload.get("run_id") or "") != expected_run_id:
        raise ControlEnvelopeError("control_evidence_run_mismatch")
    if payload.get("status") != RUN_EVIDENCE_STATUS_COMPLETED:
        raise ControlEnvelopeError("control_evidence_not_completed")
    validate_evidence_manifest_sync(candidate_root)
    return payload


def _validate_envelope_structure(envelope: dict[str, Any]) -> dict[str, Any]:
    if envelope.get("schema") != CONTROL_ENVELOPE_SCHEMA:
        raise ControlEnvelopeError("control_envelope_schema_mismatch")
    version = _require_envelope_int(envelope.get("version"), field="version")
    if version != CONTROL_ENVELOPE_VERSION:
        raise ControlEnvelopeError("control_envelope_version_mismatch")
    if envelope.get("producer") != ENTRYPOINT_PRODUCER:
        raise ControlEnvelopeError("control_envelope_producer_mismatch")
    envelope_exit = _require_envelope_int(
        envelope.get("exit_code"),
        field="exit_code",
        error_code="control_envelope_exit_code_invalid",
    )
    manifest = envelope.get("manifest")
    if not isinstance(manifest, dict):
        raise ControlEnvelopeError("control_envelope_manifest_missing")
    if manifest.get("schema") != RESULT_SCHEMA:
        raise ControlEnvelopeError("control_envelope_manifest_schema_mismatch")
    manifest_exit_raw = manifest.get("exit_code")
    if manifest_exit_raw is None:
        raise ControlEnvelopeError("control_envelope_manifest_exit_missing")
    manifest_exit = _require_envelope_int(
        manifest_exit_raw,
        field="manifest.exit_code",
        error_code="control_envelope_manifest_exit_missing",
    )
    if manifest_exit != envelope_exit:
        raise ControlEnvelopeError("control_envelope_manifest_exit_mismatch")
    return envelope


def parse_control_envelope_from_exec_output(
    stdout: str,
    *,
    expected_run_id: str,
    expected_candidate_rel: str | None,
) -> dict[str, Any]:
    envelope_line: str | None = None
    for line in stdout.splitlines():
        if line.startswith(CONTROL_ENVELOPE_PREFIX):
            envelope_line = line
    if envelope_line is None:
        raise ControlEnvelopeError("control_envelope_missing")
    try:
        envelope = json.loads(envelope_line[len(CONTROL_ENVELOPE_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise ControlEnvelopeError("control_envelope_truncated", detail=str(exc)) from exc
    if not isinstance(envelope, dict):
        raise ControlEnvelopeError("control_envelope_invalid")
    envelope = _validate_envelope_structure(envelope)
    if str(envelope.get("run_id") or "") != expected_run_id:
        raise ControlEnvelopeError("control_envelope_run_mismatch")
    if expected_candidate_rel is not None and str(envelope.get("candidate_rel") or "") != expected_candidate_rel:
        raise ControlEnvelopeError("control_envelope_candidate_mismatch")
    return envelope


def accept_control_evidence(
    envelope: dict[str, Any],
    *,
    host: HostAttestationContext,
    expected_run_id: str,
    expected_candidate_rel: str | None,
    expected_node_id: str,
    expected_test_entity_id: str,
    expected_container_id: str,
) -> dict[str, Any]:
    envelope = _validate_envelope_structure(envelope)
    if str(envelope.get("run_id") or "") != expected_run_id:
        raise ControlEnvelopeError("control_envelope_run_mismatch")
    if expected_candidate_rel is not None and str(envelope.get("candidate_rel") or "") != expected_candidate_rel:
        raise ControlEnvelopeError("control_envelope_candidate_mismatch")

    host_attestation = build_host_attestation(host)
    if host_attestation.get("container_id") != expected_container_id:
        raise ControlEnvelopeError("control_evidence_container_mismatch")
    if host_attestation.get("node_id") != expected_node_id:
        raise ControlEnvelopeError("control_evidence_node_mismatch")
    if host_attestation.get("test_entity_id") != expected_test_entity_id:
        raise ControlEnvelopeError("control_evidence_test_entity_mismatch")
    if host.execution_state != EXECUTION_EXITED:
        raise ControlEnvelopeError("control_evidence_execution_state_mismatch")
    if host.exec_exit_code is None:
        raise ControlEnvelopeError("control_evidence_exec_exit_missing")

    envelope_exit = _require_envelope_int(
        envelope.get("exit_code"),
        field="exit_code",
        error_code="control_envelope_exit_code_invalid",
    )
    exec_exit = int(host.exec_exit_code)
    if envelope_exit != exec_exit:
        raise ControlEnvelopeError("control_evidence_exec_exit_mismatch")

    manifest = envelope.get("manifest") or {}
    manifest_exit_raw = manifest.get("exit_code")
    if manifest_exit_raw is None:
        raise ControlEnvelopeError("control_envelope_manifest_exit_missing")
    manifest_exit = _require_envelope_int(
        manifest_exit_raw,
        field="manifest.exit_code",
        error_code="control_envelope_manifest_exit_missing",
    )
    if manifest_exit != exec_exit:
        raise ControlEnvelopeError("control_evidence_manifest_exit_mismatch")

    return {
        "schema": CONTROL_EVIDENCE_SCHEMA,
        "run_id": expected_run_id,
        "status": RUN_EVIDENCE_STATUS_COMPLETED,
        "candidate_rel": expected_candidate_rel,
        "envelope": envelope,
        "host_attestation": host_attestation,
    }


def persist_control_evidence(candidate_root: Path, evidence: dict[str, Any]) -> None:
    manifest = (evidence.get("envelope") or {}).get("manifest") or {}
    publish_run_evidence(
        candidate_root,
        authoritative=evidence,
        manifest_mirror=manifest,
    )


def persist_control_envelope(candidate_root: Path, envelope: dict[str, Any]) -> None:
    """Backward-compatible persist when only envelope is available."""
    persist_control_evidence(
        candidate_root,
        {
            "schema": CONTROL_EVIDENCE_SCHEMA,
            "envelope": envelope,
            "host_attestation": {},
        },
    )

