#!/usr/bin/env python3
"""Controller-side evidence publication from untrusted worker stdout."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("bridle.trusted_evidence_controller")

SCRIPT_DIR = Path(__file__).resolve().parent

CRITICAL_EVIDENCE_PREFIX = "BRIDLE_CRITICAL_EVIDENCE:"
SENTINEL_READY_PREFIX = "BRIDLE_SENTINEL_READY:"
SENTINEL_REQUEST_PREFIX = "BRIDLE_SENTINEL_REQUEST:"
RUN_REGISTER_PREFIX = "BRIDLE_RUN_REGISTER:"

INNER_CANDIDATE_ROOT = "/bridle-candidate"
REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{16}")


def _validated_request_id(
    value: Any,
    *,
    missing_error: str,
    invalid_error: str,
) -> str:
    request_id = str(value or "").strip()
    if not request_id:
        raise RuntimeError(missing_error)
    if REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        raise RuntimeError(invalid_error)
    return request_id


def _ack_path(ack_dir: Path, request_id: str) -> Path:
    resolved_dir = ack_dir.resolve()
    resolved_path = (resolved_dir / f"{request_id}.json").resolve()
    if resolved_path.parent != resolved_dir:
        raise RuntimeError("sentinel_ack_path_invalid")
    return resolved_path


def _load_sentinel_registry(trusted_scripts: Path):
    spec = importlib.util.spec_from_file_location(
        "bridle_sentinel_registry",
        trusted_scripts / "sentinel_registry.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bridle_sentinel_registry"] = module
    spec.loader.exec_module(module)
    return module


_DOCKER_TEST_SUPPORT_PKG = "bridle_docker_test_support"


def _docker_test_support_dir(trusted_pythonpath: Path) -> Path:
    """Locate backend/tests/agent/container from a trusted_pythonpath ending in backend/src."""
    if trusted_pythonpath.name == "src" and trusted_pythonpath.parent.name == "backend":
        return trusted_pythonpath.parent / "tests" / "agent" / "container"
    return trusted_pythonpath / "tests" / "agent" / "container"


def _ensure_docker_test_support_package(trusted_pythonpath: Path) -> Path:
    """Load backend/tests/agent/container as a synthetic package for relative imports."""
    support_dir = _docker_test_support_dir(trusted_pythonpath)
    if _DOCKER_TEST_SUPPORT_PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _DOCKER_TEST_SUPPORT_PKG,
            support_dir / "__init__.py",
            submodule_search_locations=[str(support_dir)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_DOCKER_TEST_SUPPORT_PKG] = module
        spec.loader.exec_module(module)
    return support_dir


def _load_docker_evidence(trusted_pythonpath: Path):
    support_dir = _ensure_docker_test_support_package(trusted_pythonpath)
    name = f"{_DOCKER_TEST_SUPPORT_PKG}.docker_evidence"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, support_dir / "docker_evidence.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_docker_test_resources(trusted_pythonpath: Path):
    support_dir = _ensure_docker_test_support_package(trusted_pythonpath)
    name = f"{_DOCKER_TEST_SUPPORT_PKG}.docker_test_resources"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, support_dir / "docker_test_resources.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _record_digest(record: Any) -> str:
    payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _control_payload(line: str, prefix: str) -> str | None:
    index = line.find(prefix)
    if index < 0:
        return None
    return line[index + len(prefix) :]


def _lstat_rejects_link(path: Path) -> os.stat_result:
    """lstat a path and reject symlink/reparse components anywhere along the chain."""
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(
            f"sentinel_component_lstat_failed path={path} errno={exc.errno}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise RuntimeError(f"sentinel_component_is_link path={path}")
    return metadata


def _is_reparse_point(metadata: os.stat_result) -> bool:
    """Detect Windows reparse points that lstat may not classify as S_ISLNK."""
    if os.name != "nt":
        return False
    return bool(getattr(metadata, "st_reparse_tag", 0))


def _lstat_component_walk(root: Path, relative: str) -> Path:
    """Walk relative components under root, lstat-ing each one and rejecting links."""
    root_meta = _lstat_rejects_link(root)
    if not stat.S_ISDIR(root_meta.st_mode):
        raise RuntimeError(f"sentinel_candidate_root_not_directory path={root}")
    current = root
    for component in relative.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            raise RuntimeError(f"sentinel_candidate_relative_escape value={relative!r}")
        current = current / component
        _lstat_rejects_link(current)
    return current


def resolve_candidate_relative_path(candidate_root: Path, candidate_relative: str) -> Path:
    """Resolve a candidate-relative path to a host path, rejecting any symlink/reparse component.

    The walk is component-wise lstat based: no resolve() that would follow links,
    so an intermediate symlink inside the candidate tree cannot redirect the
    sentinel registration to a different on-disk object.
    """
    relative = candidate_relative.strip().replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        raise RuntimeError(f"sentinel_candidate_relative_invalid value={candidate_relative!r}")
    final = _lstat_component_walk(candidate_root, relative)
    # Confirm the non-resolved abspath stays under candidate_root without following links.
    root_abs = os.path.abspath(candidate_root)
    final_abs = os.path.abspath(final)
    root_prefix = root_abs.rstrip(os.sep) + os.sep
    if final_abs != root_abs and not final_abs.startswith(root_prefix):
        raise RuntimeError(f"sentinel_candidate_relative_escape value={candidate_relative!r}")
    return Path(final_abs)


def resolve_container_path_to_host(container_path: str, candidate_root: Path) -> Path:
    """Map a container-internal absolute path (under /bridle-candidate) to a host path.

    Worker processes send container-absolute paths so they never need to know the
    host checkout root. The controller strips the inner candidate prefix and walks
    the remaining relative components with the same lstat discipline.
    """
    text = container_path.strip()
    if not text:
        raise RuntimeError("sentinel_container_path_empty")
    prefix = INNER_CANDIDATE_ROOT
    if not text.startswith(prefix):
        raise RuntimeError(f"sentinel_container_path_outside_candidate value={container_path!r}")
    remainder = text[len(prefix):].lstrip("/")
    if not remainder:
        raise RuntimeError(f"sentinel_container_path_is_root value={container_path!r}")
    return resolve_candidate_relative_path(candidate_root, remainder)


def _map_attack_targets_to_host(primary: dict[str, Any], *, candidate_root: Path) -> None:
    """Translate container-internal symlink targets to host paths for sentinel validation."""
    results = primary.get("attack_results")
    if not isinstance(results, list):
        return
    for item in results:
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        if not isinstance(target, str) or not target.startswith(INNER_CANDIDATE_ROOT):
            continue
        try:
            mapped = resolve_container_path_to_host(target, candidate_root)
        except RuntimeError:
            continue
        item["target"] = str(mapped)


def register_sentinel_request(
    payload: dict[str, Any],
    *,
    ctx: Any,
    trusted_scripts: Path,
) -> str:
    request_id = _validated_request_id(
        payload.get("request_id"),
        missing_error="sentinel_request_missing_id",
        invalid_error="sentinel_request_invalid_id",
    )
    if request_id in ctx.handled_request_ids:
        raise RuntimeError(f"sentinel_request_replayed request_id={request_id}")
    candidate_relative = str(payload.get("candidate_relative") or payload.get("path") or "").strip()
    container_path = str(payload.get("container_path") or "").strip()
    if not candidate_relative and not container_path:
        raise RuntimeError("sentinel_request_missing_candidate_relative")
    if container_path:
        host_path = resolve_container_path_to_host(container_path, ctx.candidate_root)
    else:
        host_path = resolve_candidate_relative_path(ctx.candidate_root, candidate_relative)
    registry = _load_sentinel_registry(trusted_scripts)
    record = registry.register_external_sentinel(host_path)
    handle = f"sent-{uuid.uuid4().hex[:16]}"
    ctx.sentinel_by_handle[handle] = record.to_dict()
    ctx.handled_request_ids.add(request_id)
    if ctx.controller_ipc_dir is not None:
        ack_dir = ctx.controller_ipc_dir / "sentinel-acks"
        ack_dir.mkdir(parents=True, exist_ok=True)
        ack_path = _ack_path(ack_dir, request_id)
        ack_path.write_text(
            json.dumps(
                {
                    "status": "registered",
                    "handle": handle,
                    "record_digest": _record_digest(record),
                    "request_id": request_id,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    LOGGER.info(
        "sentinel_preregistered host_path=%s handle=%s request_id=%s",
        host_path,
        handle,
        request_id,
    )
    return handle


def poll_sentinel_request_files(
    *,
    ipc_dir: Path,
    ctx: Any,
    trusted_scripts: Path,
) -> None:
    requests_dir = ipc_dir / "sentinel-requests"
    if not requests_dir.is_dir():
        return
    for path in sorted(requests_dir.glob("*.json")):
        request_id = path.stem
        if request_id in ctx.handled_request_ids:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        register_sentinel_request(payload, ctx=ctx, trusted_scripts=trusted_scripts)


def verify_live_sentinel_evidence(
    payload: dict[str, Any],
    *,
    ctx: Any,
    trusted_scripts: Path,
) -> dict[str, Any]:
    request_id = _validated_request_id(
        payload.get("controller_request_id"),
        missing_error="sentinel_live_verification_request_missing",
        invalid_error="sentinel_live_verification_request_invalid",
    )
    if request_id in ctx.verified_sentinel_by_request:
        raise RuntimeError(
            f"sentinel_live_verification_replayed request_id={request_id}"
        )
    primary = payload.get("primary")
    if not isinstance(primary, dict):
        raise RuntimeError("sentinel_live_verification_primary_invalid")
    sentinel_handle = str(primary.get("sentinel_handle") or "").strip()
    if not sentinel_handle:
        raise RuntimeError("sentinel_live_verification_handle_missing")
    before = ctx.sentinel_by_handle.get(sentinel_handle)
    if before is None:
        raise RuntimeError("sentinel_not_preregistered_by_controller")

    registry = _load_sentinel_registry(trusted_scripts)
    host_path = Path(str(before["canonical_path"]))
    after_record = registry.register_external_sentinel(host_path)
    registry.verify_external_sentinel(host_path, before)
    proof = {
        "request_id": request_id,
        "sentinel_handle": sentinel_handle,
        "before": dict(before),
        "after": after_record.to_dict(),
    }
    ctx.verified_sentinel_by_request[request_id] = proof
    if ctx.controller_ipc_dir is not None:
        ack_dir = ctx.controller_ipc_dir / "critical-evidence-acks"
        ack_dir.mkdir(parents=True, exist_ok=True)
        ack_path = _ack_path(ack_dir, request_id)
        temporary = ack_path.with_name(f".{ack_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "sentinel_handle": sentinel_handle,
                    "status": "verified",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, ack_path)
    LOGGER.info(
        "sentinel_live_verified path=%s handle=%s request_id=%s",
        host_path,
        sentinel_handle,
        request_id,
    )
    return proof


def consume_live_sentinel_verification(
    payload: dict[str, Any],
    *,
    ctx: Any,
) -> dict[str, Any]:
    request_id = _validated_request_id(
        payload.get("controller_request_id"),
        missing_error="sentinel_live_verification_request_missing",
        invalid_error="sentinel_live_verification_request_invalid",
    )
    if request_id in ctx.consumed_sentinel_verification_requests:
        raise RuntimeError(
            f"sentinel_live_verification_already_consumed request_id={request_id}"
        )
    proof = ctx.verified_sentinel_by_request.get(request_id)
    if proof is None:
        raise RuntimeError(
            f"sentinel_live_verification_missing request_id={request_id}"
        )
    primary = payload.get("primary")
    sentinel_handle = (
        str(primary.get("sentinel_handle") or "").strip()
        if isinstance(primary, dict)
        else ""
    )
    if not sentinel_handle or proof.get("sentinel_handle") != sentinel_handle:
        raise RuntimeError(
            f"sentinel_live_verification_handle_mismatch request_id={request_id}"
        )
    ctx.consumed_sentinel_verification_requests.add(request_id)
    return dict(proof)


def handle_controller_line(
    line: str,
    *,
    ctx: Any,
    trusted_scripts: Path,
) -> None:
    if _control_payload(line, RUN_REGISTER_PREFIX) is not None:
        raise RuntimeError("run_register_from_candidate_rejected")

    sentinel_request = _control_payload(line, SENTINEL_REQUEST_PREFIX)
    if sentinel_request is not None:
        register_sentinel_request(json.loads(sentinel_request), ctx=ctx, trusted_scripts=trusted_scripts)
        return
    sentinel_ready = _control_payload(line, SENTINEL_READY_PREFIX)
    if sentinel_ready is not None:
        registry = _load_sentinel_registry(trusted_scripts)
        payload = json.loads(sentinel_ready)
        candidate_relative = str(payload.get("candidate_relative") or payload.get("path") or "").strip()
        container_path = str(payload.get("container_path") or "").strip()
        if container_path:
            host_path = resolve_container_path_to_host(container_path, ctx.candidate_root)
        else:
            host_path = resolve_candidate_relative_path(ctx.candidate_root, candidate_relative)
        record = registry.register_external_sentinel(host_path)
        handle = f"sent-{uuid.uuid4().hex[:16]}"
        ctx.sentinel_by_handle[handle] = record.to_dict()
        return

    critical = _control_payload(line, CRITICAL_EVIDENCE_PREFIX)
    if critical is None or not critical.startswith('{"'):
        return
    payload = json.loads(critical)
    primary = payload.get("primary")
    if (
        payload.get("test_key") == "link_attack"
        and isinstance(primary, dict)
        and primary.get("sentinel_handle")
    ):
        verify_live_sentinel_evidence(
            payload,
            ctx=ctx,
            trusted_scripts=trusted_scripts,
        )


def mark_evidence_run_started(*, trusted_pythonpath: Path) -> None:
    if os.environ.get("BRIDLE_RUN_DOCKER_TESTS") != "1" or os.name == "nt":
        return
    de = _load_docker_evidence(trusted_pythonpath)
    de.begin_docker_evidence_session()


def _controller_teardown(
    primary: dict[str, Any],
    *,
    trusted_pythonpath: Path,
    ctx: Any,
):
    it_run_id = str(primary.get("it_run_id") or "").strip()
    if not it_run_id:
        raise RuntimeError("teardown_run_id_missing")
    if ctx.issued_it_run_id and it_run_id != ctx.issued_it_run_id:
        raise RuntimeError(f"teardown_it_run_id_mismatch it_run_id={it_run_id}")
    if ctx.lease_id:
        ctx.lease_registry.assert_teardown_allowed(ctx.lease_id, it_run_id)
    dtr = _load_docker_test_resources(trusted_pythonpath)
    assert_run_teardown_clean = dtr.assert_run_teardown_clean
    finalize_run_teardown = dtr.finalize_run_teardown

    previous_docker_host = os.environ.get("DOCKER_HOST")
    if ctx.isolated_docker_host:
        os.environ["DOCKER_HOST"] = ctx.isolated_docker_host
    try:
        teardown = finalize_run_teardown(it_run_id)
        assert_run_teardown_clean(teardown)
    finally:
        if previous_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = previous_docker_host
    return teardown


def _read_test_event(ctx: Any, event_type: str, test_key: str) -> dict[str, Any] | None:
    if ctx.controller_ipc_dir is None:
        return None
    path = ctx.controller_ipc_dir / "test-events" / f"{event_type}_{test_key}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _verify_test_event_chain(ctx: Any, test_key: str, *, claimed_node_id: str) -> dict[str, Any]:
    """Validate that the trusted observer recorded a real passed execution for test_key.

    Returns a dict with:
      - verified: bool
      - reason: str (failure reason when not verified)
      - test_node_id: str (from the trusted observer, not from candidate stdout)
    """
    expected_nonce = ctx.critical_test_nonces.get(test_key) if ctx.critical_test_nonces else None
    if not expected_nonce:
        return {"verified": False, "reason": "nonce_not_issued", "test_node_id": ""}
    collection = _read_test_event(ctx, "collection", test_key)
    if collection is None:
        return {"verified": False, "reason": "collection_event_missing", "test_node_id": ""}
    if collection.get("nonce") != expected_nonce:
        return {"verified": False, "reason": "collection_nonce_mismatch", "test_node_id": ""}
    if collection.get("collected") is not True:
        return {"verified": False, "reason": "test_not_collected", "test_node_id": ""}
    started = _read_test_event(ctx, "started", test_key)
    if started is None:
        return {"verified": False, "reason": "started_event_missing", "test_node_id": ""}
    if started.get("nonce") != expected_nonce:
        return {"verified": False, "reason": "started_nonce_mismatch", "test_node_id": ""}
    finished = _read_test_event(ctx, "finished", test_key)
    if finished is None:
        return {"verified": False, "reason": "finished_event_missing", "test_node_id": ""}
    if finished.get("nonce") != expected_nonce:
        return {"verified": False, "reason": "finished_nonce_mismatch", "test_node_id": ""}
    if finished.get("outcome") != "passed":
        return {
            "verified": False,
            "reason": f"test_outcome_not_passed:{finished.get('outcome')}",
            "test_node_id": str(finished.get("test_node_id") or ""),
        }
    trusted_node_id = str(finished.get("test_node_id") or "")
    if claimed_node_id and claimed_node_id != trusted_node_id:
        return {
            "verified": False,
            "reason": "node_id_mismatch",
            "test_node_id": trusted_node_id,
        }
    if test_key in ctx.consumed_test_event_keys:
        return {"verified": False, "reason": "test_event_already_consumed", "test_node_id": trusted_node_id}
    ctx.consumed_test_event_keys.add(test_key)
    return {"verified": True, "reason": "", "test_node_id": trusted_node_id}


def publish_from_worker_stdout(
    stdout: str,
    *,
    trusted_scripts: Path,
    trusted_pythonpath: Path,
    pytest_exitstatus: int,
    ctx: Any,
) -> int:
    if os.environ.get("BRIDLE_RUN_DOCKER_TESTS") != "1" or os.name == "nt":
        return pytest_exitstatus
    de = _load_docker_evidence(trusted_pythonpath)

    seen_test_keys: set[str] = set()
    for line in stdout.splitlines():
        critical = _control_payload(line, CRITICAL_EVIDENCE_PREFIX)
        if critical is None:
            continue
        payload = json.loads(critical)
        test_key = str(payload["test_key"])
        primary = dict(payload["primary"])
        sentinel_handle = primary.pop("sentinel_handle", None)
        if sentinel_handle and test_key == "link_attack":
            proof = consume_live_sentinel_verification(payload, ctx=ctx)
            primary["sentinel_before"] = dict(proof["before"])
            primary["sentinel_after"] = dict(proof["after"])
            _map_attack_targets_to_host(primary, candidate_root=ctx.candidate_root)
        seen_test_keys.add(test_key)
        claimed_status = str(payload.get("status") or "")
        claimed_node_id = str(payload.get("test_node_id") or "")
        verification = _verify_test_event_chain(ctx, test_key, claimed_node_id=claimed_node_id)
        trusted_node_id = verification["test_node_id"] or claimed_node_id
        if not verification["verified"]:
            de.publish_failed_evidence(
                test_key,
                test_node_id=trusted_node_id or claimed_node_id,
                image_digest=str(payload.get("image_digest") or ""),
                primary=primary,
                error=f"controller_test_event_verification_failed:{verification['reason']}",
            )
            continue
        if claimed_status != "passed":
            de.publish_failed_evidence(
                test_key,
                test_node_id=trusted_node_id,
                image_digest=str(payload.get("image_digest") or ""),
                primary=primary,
                error=str(payload.get("error") or "worker_primary_failed"),
            )
            continue
        teardown = _controller_teardown(primary, trusted_pythonpath=trusted_pythonpath, ctx=ctx)
        de.publish_passed_evidence(
            test_key,
            test_node_id=trusted_node_id,
            image_digest=str(payload.get("image_digest") or ""),
            primary=primary,
            teardown_result=teardown,
        )
    # Critical tests that never produced any stdout evidence line (e.g. skipped,
    # pytest.exit early, or import-time crash) must still be recorded as failed.
    for missing_key in de.CRITICAL_TEST_KEYS - seen_test_keys:
        verification = _verify_test_event_chain(ctx, missing_key, claimed_node_id="")
        de.publish_failed_evidence(
            missing_key,
            test_node_id=verification.get("test_node_id", ""),
            image_digest="",
            primary={"error": "no_worker_evidence_line"},
            error=f"controller_no_evidence_line:{verification['reason']}",
        )
    de.flush_session_evidence(pytest_exitstatus=pytest_exitstatus)
    return pytest_exitstatus


def wait_for_sentinel_ack(controller_ipc_dir: Path, request_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request_id = _validated_request_id(
        request_id,
        missing_error="sentinel_ack_request_missing",
        invalid_error="sentinel_ack_request_invalid",
    )
    ack_path = _ack_path(controller_ipc_dir / "sentinel-acks", request_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ack_path.is_file():
            payload = json.loads(ack_path.read_text(encoding="utf-8"))
            if payload.get("status") == "registered" and payload.get("handle"):
                return payload
        time.sleep(0.05)
    raise TimeoutError(f"sentinel_ack_timeout request_id={request_id}")
