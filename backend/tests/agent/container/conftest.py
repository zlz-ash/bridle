"""Shared pytest hooks for container integration tests.

The workspace fixture here is a twin of the one in ``backend/tests/
conftest.py`` because pytest's ``confcutdir`` excludes the parent conftest
from container tests. Both delegate to ``_workspace_lifecycle`` so they
share the same creation, identity-registration and ACL-baseline teardown,
and the same zero-leftover contract on Windows and POSIX.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from bridle.config import set_workspace
from tests._workspace_lifecycle import (
    create_workspace,
    teardown_workspace,
)

from .docker_evidence import (
    begin_docker_evidence_session,
    docker_gate_enabled,
    flush_session_evidence,
)
from .docker_gate_paths import (
    assert_critical_tests_collected,
    is_critical_docker_item,
)

_skipped_critical: list[str] = []
_skipped_boundary: list[str] = []
TEST_WORKSPACES_ROOT = Path(__file__).resolve().parents[3] / ".test-workspaces"
BOUNDARY_TEST_FILE = "test_dind_boundary_isolation.py"
logger = logging.getLogger("bridle.test")


@pytest.fixture
def test_workspace(request: pytest.FixtureRequest) -> Path:
    """Workspace fixture for docker integration tests.

    confcutdir excludes ``backend/tests/conftest.py``, so this fixture
    re-creates the same lifecycle via ``_workspace_lifecycle``. Teardown
    verifies identity, restores the ACL baseline and deletes; cleanup
    failure fails the test with a diagnostic.
    """
    test_name = request.node.name
    ws, identity = create_workspace(test_name, TEST_WORKSPACES_ROOT, with_git=True)
    set_workspace(ws)
    logger.debug("container test workspace created: %s", ws)
    yield ws
    cleanup_error = teardown_workspace(ws, identity)
    if cleanup_error:
        raise AssertionError(
            f"workspace cleanup failed for {ws}: {cleanup_error}"
        )


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
    if not report.skipped or not _gate_active():
        return
    if is_critical_docker_item(item):
        _skipped_critical.append(f"{item.nodeid}::{report.when}")
    if BOUNDARY_TEST_FILE in str(getattr(item, "fspath", "")):
        _skipped_boundary.append(f"{item.nodeid}::{report.when}")


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
    if _skipped_boundary and os.name != "nt":
        raise pytest.fail(
            "DinD boundary isolation tests were skipped on Linux: " + ", ".join(_skipped_boundary)
        )
    del session
