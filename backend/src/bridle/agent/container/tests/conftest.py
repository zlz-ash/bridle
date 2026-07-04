"""Shared pytest hooks for container integration tests."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

import pytest

from bridle.config import set_workspace
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
TEST_WORKSPACES_ROOT = Path(__file__).resolve().parents[5] / ".test-workspaces"
logger = logging.getLogger("bridle.test")


def _mkdir_test_workspace(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def test_workspace(request: pytest.FixtureRequest) -> Path:
    """Workspace fixture for docker integration tests when confcutdir excludes backend/conftest.py."""
    test_name = request.node.name
    safe_name = test_name
    for char in '<>:"|?*':
        safe_name = safe_name.replace(char, "_")
    safe_name = (
        safe_name.replace("[", "_")
        .replace("]", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )
    workspace = TEST_WORKSPACES_ROOT / f"{safe_name[:80]}-{uuid4().hex[:8]}"
    _mkdir_test_workspace(workspace)
    git_dir = workspace / ".git" / "refs" / "heads"
    git_dir.mkdir(parents=True, exist_ok=True)
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    set_workspace(workspace)
    logger.debug("container test workspace created: %s", workspace)
    return workspace


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
