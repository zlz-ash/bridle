#!/usr/bin/env python3
"""Trusted pytest plugin: independently records critical test execution events.

Loaded from the trusted scripts tree (candidate cannot modify it). Writes
collection / started / finished / sessionfinish events into the controller IPC
test-events directory so the controller can prove critical tests really ran,
instead of trusting candidate stdout evidence lines.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CRITICAL_TEST_CLASS = "TestDockerCandidateIntegration"
CRITICAL_TEST_SPEC: dict[str, str] = {
    "link_attack": "test_real_docker_recovers_after_link_attack_in_slot",
    "chmod_poison": "test_real_docker_recovers_after_rw_root_permission_poisoning",
}

EVENT_SCHEMA = "bridle.trusted_test_event/v1"


def _events_dir() -> Path | None:
    raw = os.environ.get("BRIDLE_TEST_EVENTS_DIR", "").strip()
    if not raw:
        return None
    return Path(raw)


def _nonces() -> dict[str, str]:
    raw = os.environ.get("BRIDLE_CRITICAL_TEST_NONCES", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(k): str(v) for k, v in payload.items()}


def _match_test_key(node_id: str) -> str | None:
    base = node_id.split("[", 1)[0]
    for test_key, method in CRITICAL_TEST_SPEC.items():
        expected_suffix = f"{CRITICAL_TEST_CLASS}::{method}"
        if base.endswith(expected_suffix):
            return test_key
    return None


def _write_event(event_type: str, test_key: str, *, data: dict[str, Any]) -> None:
    directory = _events_dir()
    if directory is None:
        return
    nonces = _nonces()
    nonce = nonces.get(test_key)
    if not nonce:
        return
    payload: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "event_type": event_type,
        "test_key": test_key,
        "nonce": nonce,
        "worker_pid": os.getpid(),
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "monotonic_ns": time.monotonic_ns(),
        **data,
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event_type}_{test_key}.json"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _write_session_event(event_type: str, *, data: dict[str, Any]) -> None:
    directory = _events_dir()
    if directory is None:
        return
    payload: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "event_type": event_type,
        "worker_pid": os.getpid(),
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "monotonic_ns": time.monotonic_ns(),
        **data,
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event_type}.json"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Record which critical tests were collected; flag missing ones."""
    found: dict[str, str] = {}
    for item in items:
        node_id = getattr(item, "nodeid", "")
        test_key = _match_test_key(node_id)
        if test_key is not None:
            found[test_key] = node_id
    for test_key in CRITICAL_TEST_SPEC:
        _write_event(
            "collection",
            test_key,
            data={
                "collected": test_key in found,
                "test_node_id": found.get(test_key, ""),
            },
        )


def pytest_runtest_setup(item: Any) -> None:
    node_id = getattr(item, "nodeid", "")
    test_key = _match_test_key(node_id)
    if test_key is None:
        return
    _write_event("started", test_key, data={"test_node_id": node_id})


def pytest_runtest_makereport(item: Any, report: Any) -> None:
    node_id = getattr(item, "nodeid", "")
    test_key = _match_test_key(node_id)
    if test_key is None:
        return
    if getattr(report, "when", "") != "call":
        return
    outcome = getattr(report, "outcome", "unknown")
    _write_event(
        "finished",
        test_key,
        data={
            "test_node_id": node_id,
            "outcome": outcome,
            "longrepr": str(getattr(report, "longrepr", ""))[:4000],
        },
    )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    _write_session_event(
        "sessionfinish",
        data={
            "exitstatus": int(exitstatus),
            "session_pid": os.getpid(),
        },
    )
