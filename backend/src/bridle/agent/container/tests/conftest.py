"""Shared pytest hooks for container integration tests."""
from __future__ import annotations

import os

import pytest

from bridle.agent.container.tests.docker_evidence import (
    begin_docker_evidence_session,
    docker_gate_enabled,
    flush_session_evidence,
)
from bridle.agent.container.tests.docker_gate_paths import (
    assert_critical_tests_collected,
    is_critical_docker_item,
)

_skipped_critical: list[str] = []


def _gate_active() -> bool:
    return docker_gate_enabled()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items) -> None:
    del config
    if os.environ.get("BRIDLE_CANDIDATE_WORKER") == "1":
        return
    if not _gate_active():
        return
    assert_critical_tests_collected(items)
    begin_docker_evidence_session()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not report.skipped or not _gate_active() or not is_critical_docker_item(item):
        return
    _skipped_critical.append(f"{item.nodeid}::{report.when}")


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("BRIDLE_CANDIDATE_WORKER") == "1":
        return
    flush_session_evidence(pytest_exitstatus=exitstatus)
    if not _gate_active():
        return
    if _skipped_critical:
        raise pytest.fail(
            "Critical Docker POSIX tests were skipped on Linux: " + ", ".join(_skipped_critical)
        )
    del session
