#!/usr/bin/env python3
"""Controller-side evidence publication from untrusted worker stdout."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
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


def _record_digest(record: Any) -> str:
    payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _control_payload(line: str, prefix: str) -> str | None:
    index = line.find(prefix)
    if index < 0:
        return None
    return line[index + len(prefix) :]


def resolve_candidate_relative_path(candidate_root: Path, candidate_relative: str) -> Path:
    relative = candidate_relative.strip().replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        raise RuntimeError(f"sentinel_candidate_relative_invalid value={candidate_relative!r}")
    host_path = (candidate_root / relative).resolve()
    try:
        host_path.relative_to(candidate_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"sentinel_candidate_relative_escape value={candidate_relative!r}") from exc
    if host_path.is_symlink():
        raise RuntimeError(f"sentinel_candidate_relative_symlink value={candidate_relative!r}")
    return host_path


def register_sentinel_request(
    payload: dict[str, Any],
    *,
    ctx: Any,
    trusted_scripts: Path,
) -> str:
    registry = _load_sentinel_registry(trusted_scripts)
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("sentinel_request_missing_id")
    if request_id in ctx.handled_request_ids:
        raise RuntimeError(f"sentinel_request_replayed request_id={request_id}")
    candidate_relative = str(payload.get("candidate_relative") or payload.get("path") or "").strip()
    if not candidate_relative:
        raise RuntimeError("sentinel_request_missing_candidate_relative")
    host_path = resolve_candidate_relative_path(ctx.candidate_root, candidate_relative)
    record = registry.register_external_sentinel(host_path)
    handle = f"sent-{uuid.uuid4().hex[:16]}"
    ctx.sentinel_by_handle[handle] = record.to_dict()
    ctx.handled_request_ids.add(request_id)
    if ctx.controller_ipc_dir is not None:
        ack_dir = ctx.controller_ipc_dir / "sentinel-acks"
        ack_dir.mkdir(parents=True, exist_ok=True)
        ack_path = ack_dir / f"{request_id}.json"
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
        host_path = resolve_candidate_relative_path(ctx.candidate_root, candidate_relative)
        record = registry.register_external_sentinel(host_path)
        handle = f"sent-{uuid.uuid4().hex[:16]}"
        ctx.sentinel_by_handle[handle] = record.to_dict()


def mark_evidence_run_started(*, trusted_pythonpath: Path) -> None:
    if os.environ.get("BRIDLE_RUN_DOCKER_TESTS") != "1" or os.name == "nt":
        return
    sys.path.insert(0, str(trusted_pythonpath))
    from bridle.agent.container.tests import docker_evidence as de

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
    sys.path.insert(0, str(trusted_pythonpath))
    from bridle.agent.container.tests.docker_test_resources import assert_run_teardown_clean, finalize_run_teardown

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
    sys.path.insert(0, str(trusted_pythonpath))
    from bridle.agent.container.tests import docker_evidence as de

    registry = _load_sentinel_registry(trusted_scripts)
    for line in stdout.splitlines():
        critical = _control_payload(line, CRITICAL_EVIDENCE_PREFIX)
        if critical is None:
            continue
        payload = json.loads(critical)
        test_key = str(payload["test_key"])
        primary = dict(payload["primary"])
        sentinel_handle = primary.pop("sentinel_handle", None)
        if sentinel_handle and test_key == "link_attack":
            before = ctx.sentinel_by_handle.get(str(sentinel_handle))
            if before is None:
                raise RuntimeError("sentinel_not_preregistered_by_controller")
            host_path = Path(str(before["canonical_path"]))
            after_record = registry.register_external_sentinel(host_path)
            registry.verify_external_sentinel(host_path, before)
            primary["sentinel_before"] = dict(before)
            primary["sentinel_after"] = after_record.to_dict()
        if payload.get("status") == "passed":
            teardown = _controller_teardown(primary, trusted_pythonpath=trusted_pythonpath, ctx=ctx)
            de.publish_passed_evidence(
                test_key,
                test_node_id=payload["test_node_id"],
                image_digest=payload["image_digest"],
                primary=primary,
                teardown_result=teardown,
            )
        else:
            de.publish_failed_evidence(
                test_key,
                test_node_id=payload["test_node_id"],
                image_digest=payload["image_digest"],
                primary=primary,
                error=str(payload.get("error") or "worker_primary_failed"),
            )
    de.flush_session_evidence(pytest_exitstatus=pytest_exitstatus)
    return pytest_exitstatus


def wait_for_sentinel_ack(controller_ipc_dir: Path, request_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    ack_path = controller_ipc_dir / "sentinel-acks" / f"{request_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ack_path.is_file():
            payload = json.loads(ack_path.read_text(encoding="utf-8"))
            if payload.get("status") == "registered" and payload.get("handle"):
                return payload
        time.sleep(0.05)
    raise TimeoutError(f"sentinel_ack_timeout request_id={request_id}")
