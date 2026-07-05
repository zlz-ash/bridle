"""Versioned, atomic Docker integration evidence for Linux CI gates."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("bridle")

DOCKER_EVIDENCE_ENTRY_SCHEMA = "bridle.docker_integration_evidence/v1"
DOCKER_EVIDENCE_SUMMARY_SCHEMA = "bridle.docker_integration_session/v1"
DOCKER_EVIDENCE_VERSION = 1
PRODUCER_VERSION = "bridle.docker_evidence/v1"

EVIDENCE_STATUS_PENDING = "pending"
EVIDENCE_STATUS_PASSED = "passed"
EVIDENCE_STATUS_FAILED = "failed"

CRITICAL_TEST_KEYS = frozenset({"link_attack", "chmod_poison"})
CRITICAL_TEST_CLASS = "TestDockerCandidateIntegration"
CRITICAL_TEST_SPEC: dict[str, str] = {
    "link_attack": "test_real_docker_recovers_after_link_attack_in_slot",
    "chmod_poison": "test_real_docker_recovers_after_rw_root_permission_poisoning",
}
RW_ROOT_NAMES = ("project", "output", "diagnostics")
LINK_ATTACK_LINK_NAMES = ("attack.txt", "escape.txt")
APPROVED_ENTRY_COMMANDS: dict[str, str] = {
    "link_attack": "python -m pytest tests/test_link_attack.py -q -s --capture=no",
    "chmod_poison": (
        "python -m pytest tests/test_chmod_poison.py -q -s --capture=no "
        "-p no:cacheprovider --basetemp=/tmp/bridle-chmod-pytest"
    ),
}
LINK_ATTACK_LINK_PATHS: dict[str, str] = {
    "attack.txt": "/workspace/project/attack.txt",
    "escape.txt": "/workspace/output/escape.txt",
}
CHMOD_MOUNT_PATHS: dict[str, str] = {
    "project": "/workspace/project",
    "output": "/workspace/output",
    "diagnostics": "/workspace/diagnostics",
}

_SESSION: dict[str, Any] | None = None
_PENDING: dict[str, dict[str, Any]] = {}
_PUBLISHED: list[dict[str, Any]] = []


class DockerEvidenceError(ValueError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


def docker_gate_enabled() -> bool:
    return os.environ.get("BRIDLE_RUN_DOCKER_TESTS") == "1" and os.name != "nt"


def evidence_dir() -> Path | None:
    raw = os.environ.get("BRIDLE_DOCKER_EVIDENCE_DIR", "").strip()
    if not raw:
        return None
    return Path(raw)


def canonical_entry_digest(entry: dict[str, Any]) -> str:
    payload = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _nodeid_base(nodeid: str) -> str:
    return nodeid.split("[", 1)[0]


def validate_test_node_id_for_key(test_key: str, test_node_id: str) -> None:
    if test_key not in CRITICAL_TEST_SPEC:
        raise DockerEvidenceError("docker_evidence_unknown_test_key", detail=test_key)
    if not isinstance(test_node_id, str) or not test_node_id.strip():
        raise DockerEvidenceError("docker_evidence_missing_field", detail="test_node_id")
    base = _nodeid_base(test_node_id.strip())
    expected_suffix = f"{CRITICAL_TEST_CLASS}::{CRITICAL_TEST_SPEC[test_key]}"
    if not base.endswith(expected_suffix):
        raise DockerEvidenceError("docker_evidence_test_node_mismatch", detail=test_key)
    file_part = base.split("::", 1)[0]
    from bridle.agent.container.tests.docker_gate_paths import (
        canonical_integration_test_path,
        normalize_integration_test_path,
        resolve_nodeid_file_part,
    )

    resolved = resolve_nodeid_file_part(file_part)
    canonical = canonical_integration_test_path()
    if canonical is not None:
        if resolved is None or resolved != canonical:
            raise DockerEvidenceError("docker_evidence_test_node_mismatch", detail=test_key)
    elif Path(file_part).name != "test_docker_integration.py":
        raise DockerEvidenceError("docker_evidence_test_node_mismatch", detail=test_key)


def _require_nonempty_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}:empty")
    return value.strip()


def _require_bool_true(value: object, *, field: str) -> None:
    if value is not True:
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}:{value!r}")


def _require_int_value(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}:type")
    return value


def _require_mode_value(value: object, *, field: str) -> int:
    mode = _require_int_value(value, field=field)
    if mode < 0 or mode > 0o777:
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}:range")
    return mode


def _require_approved_command(test_key: str, primary: dict[str, Any]) -> None:
    command = _require_nonempty_str(primary.get("entry_command"), field="entry_command")
    if command != APPROVED_ENTRY_COMMANDS[test_key]:
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"entry_command:{test_key}")


def _validate_external_target(value: object) -> str:
    target = _require_nonempty_str(value, field="target")
    normalized = posixpath.normpath(target)
    if (
        not target.startswith("/")
        or target.startswith("//")
        or target != normalized
        or normalized == "/workspace"
        or normalized.startswith("/workspace/")
    ):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_results:target_boundary")
    return target


def _validate_sentinel_record(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}:type")
    if value.get("schema") != "bridle.external_sentinel/v1":
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}:schema")
    canonical_path = _require_nonempty_str(value.get("canonical_path"), field=f"{field}.canonical_path")
    content_digest = _require_nonempty_str(value.get("content_digest"), field=f"{field}.content_digest")
    if not content_digest.startswith("sha256:"):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}.content_digest")
    for name in ("device", "inode", "mode"):
        _require_int_value(value.get(name), field=f"{field}.{name}")
    file_type = _require_nonempty_str(value.get("file_type"), field=f"{field}.file_type")
    if file_type != "file":
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"{field}.file_type")
    return value


def _validate_sentinel_identity_pair(primary: dict[str, Any]) -> None:
    before = _validate_sentinel_record(primary.get("sentinel_before"), field="sentinel_before")
    after = _validate_sentinel_record(primary.get("sentinel_after"), field="sentinel_after")
    for field in ("canonical_path", "device", "inode", "file_type", "mode", "content_digest"):
        if before.get(field) != after.get(field):
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"sentinel_mismatch:{field}")
    results = primary.get("attack_results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                target = item.get("target")
                if isinstance(target, str) and target != before.get("canonical_path"):
                    raise DockerEvidenceError(
                        "docker_evidence_primary_invalid",
                        detail="attack_results:target_not_registered_sentinel",
                    )


def _validate_link_attack_primary(primary: dict[str, Any]) -> None:
    if _require_int_value(primary.get("attack_uid"), field="attack_uid") != 1000:
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_uid:not_1000")
    _require_approved_command("link_attack", primary)
    _require_nonempty_str(primary.get("container_id"), field="container_id")
    _require_nonempty_str(primary.get("it_run_id"), field="it_run_id")
    _require_nonempty_str(primary.get("module_id"), field="module_id")
    for field in ("first_run_id", "attack_run_id", "second_run_id"):
        _require_nonempty_str(primary.get(field), field=field)
    _require_bool_true(primary.get("container_reused"), field="container_reused")
    _require_bool_true(primary.get("symlinks_removed"), field="symlinks_removed")
    _require_bool_true(primary.get("outside_secret_intact"), field="outside_secret_intact")
    _validate_sentinel_identity_pair(primary)

    results = primary.get("attack_results")
    if not isinstance(results, list) or len(results) != len(LINK_ATTACK_LINK_NAMES):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_results:shape")
    seen_names: set[str] = set()
    external_targets: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_results:item_type")
        if _require_int_value(item.get("uid"), field="uid") != 1000:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_results:uid")
        name = item.get("name")
        if name not in LINK_ATTACK_LINK_NAMES:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"attack_results:name:{name!r}")
        if name in seen_names:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"attack_results:dup:{name}")
        seen_names.add(str(name))
        link_path = _require_nonempty_str(item.get("link_path"), field="link_path")
        if link_path != LINK_ATTACK_LINK_PATHS[str(name)]:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"attack_results:path:{name}")
        target = _validate_external_target(item.get("target"))
        external_targets.add(target)
        if _require_int_value(item.get("symlink_rc"), field="symlink_rc") != 0:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"attack_results:rc:{name}")
        if item.get("lstat_is_symlink") is not True:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"attack_results:not_symlink:{name}")
    if seen_names != set(LINK_ATTACK_LINK_NAMES):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_results:missing_link")
    if len(external_targets) != 1:
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_results:target_mismatch")


def _validate_chmod_poison_primary(primary: dict[str, Any]) -> None:
    if _require_int_value(primary.get("attack_uid"), field="attack_uid") != 1000:
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="attack_uid:not_1000")
    _require_approved_command("chmod_poison", primary)
    _require_nonempty_str(primary.get("container_id"), field="container_id")
    _require_nonempty_str(primary.get("it_run_id"), field="it_run_id")
    _require_nonempty_str(primary.get("module_id"), field="module_id")
    for field in ("first_run_id", "second_run_id"):
        _require_nonempty_str(primary.get(field), field=field)
    _require_bool_true(primary.get("container_reused"), field="container_reused")

    results = primary.get("chmod_results")
    if not isinstance(results, list) or len(results) != len(RW_ROOT_NAMES):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="chmod_results:shape")
    seen_paths: set[str] = set()
    succeeded = 0
    for item in results:
        if not isinstance(item, dict):
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail="chmod_results:item_type")
        if _require_int_value(item.get("uid"), field="uid") != 1000:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail="chmod_results:uid")
        mount_path = _require_nonempty_str(item.get("path"), field="path")
        if mount_path not in CHMOD_MOUNT_PATHS.values():
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"chmod_results:path:{mount_path}")
        if mount_path in seen_paths:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"chmod_results:dup:{mount_path}")
        seen_paths.add(mount_path)
        rc = _require_int_value(item.get("rc"), field="rc")
        if rc == 0:
            succeeded += 1
            if _require_mode_value(item.get("after_mode"), field="after_mode") != 0:
                raise DockerEvidenceError("docker_evidence_primary_invalid", detail="chmod_results:after_mode")
    if seen_paths != set(CHMOD_MOUNT_PATHS.values()):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="chmod_results:missing_path")
    if succeeded == 0:
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="chmod_results:no_success")

    trusted = primary.get("trusted_modes")
    recovered = primary.get("recovered_modes")
    if not isinstance(trusted, dict) or not isinstance(recovered, dict):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="modes:type")
    if set(trusted.keys()) != set(RW_ROOT_NAMES) or set(recovered.keys()) != set(RW_ROOT_NAMES):
        raise DockerEvidenceError("docker_evidence_primary_invalid", detail="modes:keys")
    for root_name in RW_ROOT_NAMES:
        trusted_mode = _require_mode_value(trusted[root_name], field=f"trusted_modes.{root_name}")
        recovered_mode = _require_mode_value(recovered[root_name], field=f"recovered_modes.{root_name}")
        if trusted_mode != recovered_mode:
            raise DockerEvidenceError("docker_evidence_primary_invalid", detail=f"modes:mismatch:{root_name}")


def _validate_primary_contract(test_key: str, primary: object) -> None:
    if not isinstance(primary, dict):
        raise DockerEvidenceError("docker_evidence_missing_primary")
    if test_key == "link_attack":
        _validate_link_attack_primary(primary)
    elif test_key == "chmod_poison":
        _validate_chmod_poison_primary(primary)
    else:
        raise DockerEvidenceError("docker_evidence_unknown_test_key", detail=test_key)


def _validate_teardown_run_binding(test_key: str, primary: dict[str, Any], teardown: dict[str, Any]) -> None:
    it_run_id = _require_nonempty_str(primary.get("it_run_id"), field="it_run_id")
    owner_run_id = teardown.get("owner_run_id")
    if not isinstance(owner_run_id, str) or not owner_run_id.strip() or owner_run_id.strip() != it_run_id:
        raise DockerEvidenceError("docker_evidence_teardown_run_mismatch", detail=test_key)


def _resolve_github_sha() -> str:
    env_sha = os.environ.get("GITHUB_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_source_digest() -> str:
    override = os.environ.get("BRIDLE_REVIEW_SOURCE_DIGEST", "").strip()
    if override:
        return override
    from bridle.agent.container.review_image import compute_agent_source_digest, find_repo_root

    return compute_agent_source_digest(find_repo_root())


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _require_non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DockerEvidenceError("docker_evidence_invalid_count", detail=field)
    if value < 0:
        raise DockerEvidenceError("docker_evidence_invalid_count", detail=field)
    return value


def recompute_zero_leftover(teardown: dict[str, Any]) -> bool:
    counts = (
        _require_non_negative_int(teardown.get("remaining_container_count"), field="remaining_container_count"),
        _require_non_negative_int(teardown.get("remaining_image_count"), field="remaining_image_count"),
        _require_non_negative_int(
            teardown.get("remaining_image_registry_count"),
            field="remaining_image_registry_count",
        ),
        _require_non_negative_int(teardown.get("remaining_tag_registry_count"), field="remaining_tag_registry_count"),
    )
    query_failures = teardown.get("query_failures")
    if not isinstance(query_failures, list):
        raise DockerEvidenceError("docker_evidence_invalid_query_failures")
    return all(count == 0 for count in counts) and not query_failures


def _serialize_teardown(result: Any) -> dict[str, Any]:
    zero_leftover = (
        result.remaining_container_count == 0
        and result.remaining_image_count == 0
        and result.remaining_image_registry_count == 0
        and result.remaining_tag_registry_count == 0
        and not result.query_failures
    )
    payload = {
        "owner_run_id": result.owner_run_id,
        "remaining_container_count": result.remaining_container_count,
        "remaining_image_count": result.remaining_image_count,
        "remaining_image_registry_count": result.remaining_image_registry_count,
        "remaining_tag_registry_count": result.remaining_tag_registry_count,
        "query_failures": list(result.query_failures),
        "zero_leftover": zero_leftover,
    }
    if recompute_zero_leftover(payload) != zero_leftover:
        raise DockerEvidenceError("docker_evidence_teardown_serialization_inconsistent")
    return payload


def _invalidate_existing_evidence(directory: Path) -> None:
    for name in ("session-summary.json", "link_attack.json", "chmod_poison.json"):
        path = directory / name
        if not path.exists():
            continue
        tainted = path.with_name(f"{path.stem}.tainted.{uuid.uuid4().hex}.json")
        path.replace(tainted)
        logger.info("docker_evidence_invalidated", extra={"path": str(path), "tainted": str(tainted)})


def begin_docker_evidence_session() -> str | None:
    global _SESSION, _PENDING, _PUBLISHED
    directory = evidence_dir()
    if directory is None or not docker_gate_enabled():
        _SESSION = None
        _PENDING = {}
        _PUBLISHED = []
        return None

    session_id = uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    _invalidate_existing_evidence(directory)
    _SESSION = {
        "session_id": session_id,
        "github_sha": _resolve_github_sha(),
        "source_digest": _resolve_source_digest(),
        "producer_version": PRODUCER_VERSION,
        "started_at": datetime.now(tz=UTC).isoformat(),
    }
    _PENDING = {}
    _PUBLISHED = []
    logger.info("docker_evidence_session_started", extra={"session_id": session_id, "dir": str(directory)})
    return session_id


def record_pending_primary(test_key: str, primary: dict[str, Any]) -> None:
    if test_key not in CRITICAL_TEST_KEYS:
        raise DockerEvidenceError("docker_evidence_unknown_test_key", detail=test_key)
    if _SESSION is None:
        return
    _PENDING[test_key] = dict(primary)


def publish_passed_evidence(
    test_key: str,
    *,
    test_node_id: str,
    image_digest: str,
    primary: dict[str, Any],
    teardown_result: Any,
    extra: dict[str, Any] | None = None,
) -> None:
    if test_key not in CRITICAL_TEST_KEYS:
        raise DockerEvidenceError("docker_evidence_unknown_test_key", detail=test_key)
    directory = evidence_dir()
    if directory is None or _SESSION is None:
        return

    payload: dict[str, Any] = {
        "schema": DOCKER_EVIDENCE_ENTRY_SCHEMA,
        "version": DOCKER_EVIDENCE_VERSION,
        "producer": PRODUCER_VERSION,
        "complete": True,
        "status": EVIDENCE_STATUS_PASSED,
        "session_id": _SESSION["session_id"],
        "test_key": test_key,
        "test_node_id": test_node_id,
        "github_sha": _SESSION["github_sha"],
        "source_digest": _SESSION["source_digest"],
        "image_digest": image_digest,
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "pytest_outcome": "passed",
        "primary": primary,
        "teardown": _serialize_teardown(teardown_result),
        **(extra or {}),
    }
    validate_evidence_entry(payload, recompute_success=True)
    _atomic_write_json(directory / f"{test_key}.json", payload)
    _PUBLISHED.append(payload)
    _PENDING.pop(test_key, None)
    logger.info("docker_evidence_published", extra={"test_key": test_key, "status": "passed"})


def publish_failed_evidence(
    test_key: str,
    *,
    test_node_id: str,
    image_digest: str,
    primary: dict[str, Any],
    error: str,
    cleanup_failure: str | None = None,
    pytest_outcome: str = "failed",
) -> None:
    if test_key not in CRITICAL_TEST_KEYS:
        raise DockerEvidenceError("docker_evidence_unknown_test_key", detail=test_key)
    directory = evidence_dir()
    if directory is None or _SESSION is None:
        return

    payload: dict[str, Any] = {
        "schema": DOCKER_EVIDENCE_ENTRY_SCHEMA,
        "version": DOCKER_EVIDENCE_VERSION,
        "producer": PRODUCER_VERSION,
        "complete": True,
        "status": EVIDENCE_STATUS_FAILED,
        "session_id": _SESSION["session_id"],
        "test_key": test_key,
        "test_node_id": test_node_id,
        "github_sha": _SESSION["github_sha"],
        "source_digest": _SESSION["source_digest"],
        "image_digest": image_digest,
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "pytest_outcome": pytest_outcome,
        "primary": primary,
        "failure": {
            "error": error,
            "cleanup_failure": cleanup_failure,
        },
        "teardown": None,
    }
    _atomic_write_json(directory / f"{test_key}.json", payload)
    _PUBLISHED.append(payload)
    _PENDING.pop(test_key, None)
    logger.info("docker_evidence_published", extra={"test_key": test_key, "status": "failed"})


def _compute_summary_status(entries: list[dict[str, Any]], *, pytest_exitstatus: int | None) -> str:
    critical_keys = {entry["test_key"] for entry in entries}
    if (
        pytest_exitstatus not in (None, 0)
        or critical_keys != CRITICAL_TEST_KEYS
        or any(entry.get("status") != EVIDENCE_STATUS_PASSED for entry in entries)
        or any(not entry.get("complete") for entry in entries)
    ):
        return EVIDENCE_STATUS_FAILED
    for entry in entries:
        try:
            validate_evidence_entry(entry, recompute_success=True)
        except DockerEvidenceError:
            return EVIDENCE_STATUS_FAILED
    return EVIDENCE_STATUS_PASSED


def _validate_key_set(field_name: str, keys: object) -> None:
    if not isinstance(keys, (list, dict, set, frozenset)):
        raise DockerEvidenceError("docker_evidence_summary_keys_invalid", detail=field_name)
    normalized = set(keys)
    if normalized != CRITICAL_TEST_KEYS:
        raise DockerEvidenceError("docker_evidence_summary_keys_invalid", detail=field_name)


def flush_session_evidence(*, pytest_exitstatus: int | None = None) -> Path | None:
    directory = evidence_dir()
    if directory is None or _SESSION is None:
        return None

    entries = list(_PUBLISHED)
    summary_status = _compute_summary_status(entries, pytest_exitstatus=pytest_exitstatus)
    entry_digests = {entry["test_key"]: canonical_entry_digest(entry) for entry in entries}
    if summary_status == EVIDENCE_STATUS_PASSED:
        _validate_key_set("entry_digests", entry_digests)

    summary: dict[str, Any] = {
        "schema": DOCKER_EVIDENCE_SUMMARY_SCHEMA,
        "version": DOCKER_EVIDENCE_VERSION,
        "producer": PRODUCER_VERSION,
        "complete": True,
        "status": summary_status,
        "session_id": _SESSION["session_id"],
        "github_sha": _SESSION["github_sha"],
        "source_digest": _SESSION["source_digest"],
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "pytest_exitstatus": pytest_exitstatus,
        "critical_test_keys": sorted(CRITICAL_TEST_KEYS),
        "entry_digests": entry_digests,
        "entries": entries,
    }
    path = directory / "session-summary.json"
    _atomic_write_json(path, summary)
    return path


def _require_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DockerEvidenceError("docker_evidence_missing_field", detail=field)
    return value.strip()


def validate_evidence_entry(payload: dict[str, Any], *, recompute_success: bool = False) -> None:
    schema = _require_str(payload, "schema")
    if schema != DOCKER_EVIDENCE_ENTRY_SCHEMA:
        raise DockerEvidenceError("docker_evidence_schema_mismatch", detail=schema)
    version = payload.get("version")
    if version != DOCKER_EVIDENCE_VERSION:
        raise DockerEvidenceError("docker_evidence_version_mismatch", detail=str(version))
    if payload.get("producer") != PRODUCER_VERSION:
        raise DockerEvidenceError("docker_evidence_producer_mismatch")
    if payload.get("complete") is not True:
        raise DockerEvidenceError("docker_evidence_incomplete")
    status = _require_str(payload, "status")
    if status not in {EVIDENCE_STATUS_PASSED, EVIDENCE_STATUS_FAILED, EVIDENCE_STATUS_PENDING}:
        raise DockerEvidenceError("docker_evidence_invalid_status", detail=status)
    test_key = _require_str(payload, "test_key")
    if test_key not in CRITICAL_TEST_KEYS:
        raise DockerEvidenceError("docker_evidence_unknown_test_key", detail=test_key)
    _require_str(payload, "session_id")
    _require_str(payload, "source_digest")
    _require_str(payload, "image_digest")
    test_node_id = _require_str(payload, "test_node_id")

    if not recompute_success:
        return

    if status != EVIDENCE_STATUS_PASSED:
        raise DockerEvidenceError("docker_evidence_entry_not_passed", detail=test_key)
    if _require_str(payload, "pytest_outcome") != "passed":
        raise DockerEvidenceError("docker_evidence_pytest_outcome_not_passed", detail=test_key)
    validate_test_node_id_for_key(test_key, test_node_id)
    primary = payload.get("primary")
    if not isinstance(primary, dict):
        raise DockerEvidenceError("docker_evidence_missing_primary")
    _validate_primary_contract(test_key, primary)
    teardown = payload.get("teardown")
    if not isinstance(teardown, dict):
        raise DockerEvidenceError("docker_evidence_teardown_not_clean")
    _validate_teardown_run_binding(test_key, primary, teardown)
    recomputed = recompute_zero_leftover(teardown)
    declared = teardown.get("zero_leftover")
    if declared is not True or recomputed is not True:
        raise DockerEvidenceError("docker_evidence_teardown_not_clean", detail=test_key)


def _validate_entry_identity_binding(
    entry: dict[str, Any],
    *,
    summary: dict[str, Any],
    expected_source_digest: str,
    expected_image_digest: str,
    expected_github_sha: str,
) -> None:
    test_key = entry.get("test_key", "")
    session_id = _require_str(summary, "session_id")
    if _require_str(entry, "session_id") != session_id:
        raise DockerEvidenceError("docker_evidence_session_mismatch", detail=str(test_key))
    source_digest = _require_str(summary, "source_digest")
    entry_source = _require_str(entry, "source_digest")
    if entry_source != source_digest:
        raise DockerEvidenceError("docker_evidence_source_digest_mismatch", detail=str(test_key))
    if source_digest != expected_source_digest:
        raise DockerEvidenceError(
            "docker_evidence_source_digest_mismatch",
            detail=f"expected={expected_source_digest} got={source_digest}",
        )
    summary_sha = _require_str(summary, "github_sha")
    entry_sha = _require_str(entry, "github_sha")
    if entry_sha != summary_sha or summary_sha != expected_github_sha:
        raise DockerEvidenceError("docker_evidence_github_sha_mismatch", detail=str(test_key))
    if _require_str(entry, "producer") != _require_str(summary, "producer"):
        raise DockerEvidenceError("docker_evidence_producer_mismatch")
    if entry.get("version") != summary.get("version"):
        raise DockerEvidenceError("docker_evidence_version_mismatch")
    if _require_str(entry, "schema") != DOCKER_EVIDENCE_ENTRY_SCHEMA:
        raise DockerEvidenceError("docker_evidence_schema_mismatch")
    image_digest = _require_str(entry, "image_digest")
    if image_digest != expected_image_digest:
        raise DockerEvidenceError("docker_evidence_image_digest_mismatch", detail=str(test_key))


def validate_session_summary(
    payload: dict[str, Any],
    *,
    expected_source_digest: str,
    expected_image_digest: str,
    expected_github_sha: str,
) -> None:
    schema = _require_str(payload, "schema")
    if schema != DOCKER_EVIDENCE_SUMMARY_SCHEMA:
        raise DockerEvidenceError("docker_evidence_summary_schema_mismatch", detail=schema)
    if payload.get("version") != DOCKER_EVIDENCE_VERSION:
        raise DockerEvidenceError("docker_evidence_version_mismatch")
    if payload.get("producer") != PRODUCER_VERSION:
        raise DockerEvidenceError("docker_evidence_producer_mismatch")
    if payload.get("complete") is not True:
        raise DockerEvidenceError("docker_evidence_incomplete")

    exit_status = payload.get("pytest_exitstatus")
    if exit_status is None or exit_status != 0:
        raise DockerEvidenceError("docker_evidence_pytest_exit_not_zero", detail=str(exit_status))

    _validate_key_set("critical_test_keys", payload.get("critical_test_keys"))
    entry_digests = payload.get("entry_digests")
    if not isinstance(entry_digests, dict):
        raise DockerEvidenceError("docker_evidence_entry_digests_missing")
    _validate_key_set("entry_digests", entry_digests)

    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise DockerEvidenceError("docker_evidence_summary_entries_invalid")

    seen_keys: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise DockerEvidenceError("docker_evidence_summary_entry_invalid")
        validate_evidence_entry(entry, recompute_success=True)
        test_key = entry["test_key"]
        if test_key in seen_keys:
            raise DockerEvidenceError("docker_evidence_duplicate_test_key", detail=test_key)
        seen_keys.add(test_key)
        digest = canonical_entry_digest(entry)
        if entry_digests.get(test_key) != digest:
            raise DockerEvidenceError("docker_evidence_entry_digest_mismatch", detail=test_key)
        _validate_entry_identity_binding(
            entry,
            summary=payload,
            expected_source_digest=expected_source_digest,
            expected_image_digest=expected_image_digest,
            expected_github_sha=expected_github_sha,
        )

    if seen_keys != CRITICAL_TEST_KEYS:
        raise DockerEvidenceError(
            "docker_evidence_critical_tests_incomplete",
            detail=f"missing={sorted(CRITICAL_TEST_KEYS - seen_keys)}",
        )

    recomputed_status = _compute_summary_status(entries, pytest_exitstatus=exit_status)
    if recomputed_status != EVIDENCE_STATUS_PASSED:
        raise DockerEvidenceError("docker_evidence_summary_invariants_failed")
    if _require_str(payload, "status") != EVIDENCE_STATUS_PASSED:
        raise DockerEvidenceError("docker_evidence_summary_not_passed")


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DockerEvidenceError("docker_evidence_file_missing", detail=str(path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DockerEvidenceError("docker_evidence_json_invalid", detail=str(path)) from exc
    if not isinstance(payload, dict):
        raise DockerEvidenceError("docker_evidence_json_invalid", detail=str(path))
    return payload


def validate_evidence_directory_for_gate(
    directory: Path,
    *,
    expected_source_digest: str,
    expected_image_digest: str,
    expected_github_sha: str,
) -> None:
    summary = load_json_file(directory / "session-summary.json")
    validate_session_summary(
        summary,
        expected_source_digest=expected_source_digest,
        expected_image_digest=expected_image_digest,
        expected_github_sha=expected_github_sha,
    )

    entry_digests = summary.get("entry_digests")
    if not isinstance(entry_digests, dict):
        raise DockerEvidenceError("docker_evidence_entry_digests_missing")

    summary_entries = {
        entry["test_key"]: entry
        for entry in summary["entries"]
        if isinstance(entry, dict) and isinstance(entry.get("test_key"), str)
    }

    file_image_digests: set[str] = set()
    for test_key in sorted(CRITICAL_TEST_KEYS):
        file_entry = load_json_file(directory / f"{test_key}.json")
        validate_evidence_entry(file_entry, recompute_success=True)
        if file_entry.get("status") != EVIDENCE_STATUS_PASSED:
            raise DockerEvidenceError("docker_evidence_entry_not_passed", detail=test_key)

        file_digest = canonical_entry_digest(file_entry)
        if entry_digests.get(test_key) != file_digest:
            raise DockerEvidenceError("docker_evidence_entry_digest_mismatch", detail=test_key)

        summary_entry = summary_entries.get(test_key)
        if summary_entry is None:
            raise DockerEvidenceError("docker_evidence_summary_entry_missing", detail=test_key)
        if canonical_entry_digest(summary_entry) != file_digest:
            raise DockerEvidenceError("docker_evidence_summary_entry_mismatch", detail=test_key)

        _validate_entry_identity_binding(
            file_entry,
            summary=summary,
            expected_source_digest=expected_source_digest,
            expected_image_digest=expected_image_digest,
            expected_github_sha=expected_github_sha,
        )
        file_image_digests.add(_require_str(file_entry, "image_digest"))

    if len(file_image_digests) != 1 or expected_image_digest not in file_image_digests:
        raise DockerEvidenceError("docker_evidence_image_digest_mismatch", detail="mixed_entry_images")


def validate_evidence_directory(
    directory: Path,
    *,
    expected_source_digest: str,
    expected_image_digest: str,
    expected_github_sha: str,
) -> None:
    validate_evidence_directory_for_gate(
        directory,
        expected_source_digest=expected_source_digest,
        expected_image_digest=expected_image_digest,
        expected_github_sha=expected_github_sha,
    )


def collected_evidence() -> list[dict[str, Any]]:
    return list(_PUBLISHED)


def reset_evidence_state_for_tests() -> None:
    global _SESSION, _PENDING, _PUBLISHED
    _SESSION = None
    _PENDING = {}
    _PUBLISHED = []
