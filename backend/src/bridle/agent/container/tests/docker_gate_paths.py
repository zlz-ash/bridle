"""Path and node-id rules for critical Docker integration test collection."""
from __future__ import annotations

import os
from pathlib import Path

from bridle.agent.container.tests.docker_evidence import (
    CRITICAL_TEST_CLASS,
    CRITICAL_TEST_KEYS,
    CRITICAL_TEST_SPEC,
    DockerEvidenceError,
)

CANONICAL_INTEGRATION_REL_PARTS = (
    "backend",
    "src",
    "bridle",
    "agent",
    "container",
    "tests",
    "test_docker_integration.py",
)


def trusted_checkout_root() -> Path | None:
    raw = os.environ.get("BRIDLE_TRUSTED_CHECKOUT_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).resolve()


def canonical_integration_test_path() -> Path | None:
    root = trusted_checkout_root()
    if root is None:
        return None
    return (root.joinpath(*CANONICAL_INTEGRATION_REL_PARTS)).resolve()


def normalize_integration_test_path(fspath: object) -> Path | None:
    try:
        return Path(str(fspath)).resolve()
    except (OSError, ValueError):
        return None


def is_canonical_integration_test_path(fspath: object) -> bool:
    resolved = normalize_integration_test_path(fspath)
    if resolved is None:
        return False
    canonical = canonical_integration_test_path()
    if canonical is None:
        return False
    try:
        return resolved == canonical
    except OSError:
        return False


def expected_node_suffix(test_key: str) -> str:
    if test_key not in CRITICAL_TEST_SPEC:
        raise DockerEvidenceError("docker_evidence_unknown_test_key", detail=test_key)
    function_name = CRITICAL_TEST_SPEC[test_key]
    return f"{CRITICAL_TEST_CLASS}::{function_name}"


def nodeid_base(nodeid: str) -> str:
    return nodeid.split("[", 1)[0]


def canonical_node_id(test_key: str) -> str | None:
    canonical = canonical_integration_test_path()
    if canonical is None:
        return None
    return f"{canonical}::{expected_node_suffix(test_key)}"


def matches_critical_test_node(test_key: str, nodeid: str) -> bool:
    base = nodeid_base(nodeid)
    suffix = expected_node_suffix(test_key)
    if not base.endswith(suffix):
        return False
    file_part = base.split("::", 1)[0]
    resolved = normalize_integration_test_path(file_part)
    canonical = canonical_integration_test_path()
    if canonical is None or resolved is None:
        return False
    return resolved == canonical


def critical_node_ids_from_items(items: list) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {test_key: [] for test_key in sorted(CRITICAL_TEST_KEYS)}
    for item in items:
        if not is_canonical_integration_test_path(getattr(item, "fspath", "")):
            continue
        nodeid = str(getattr(item, "nodeid", ""))
        for test_key in CRITICAL_TEST_KEYS:
            if matches_critical_test_node(test_key, nodeid):
                found[test_key].append(nodeid)
    return found


def assert_critical_tests_collected(items: list) -> None:
    found = critical_node_ids_from_items(items)
    missing = [test_key for test_key, node_ids in found.items() if not node_ids]
    if missing:
        raise DockerEvidenceError(
            "docker_gate_critical_tests_not_collected",
            detail=", ".join(missing),
        )
    duplicated = [test_key for test_key, node_ids in found.items() if len(node_ids) > 1]
    if duplicated:
        raise DockerEvidenceError(
            "docker_gate_critical_tests_duplicate",
            detail=", ".join(duplicated),
        )


def is_critical_docker_item(item: object) -> bool:
    if not is_canonical_integration_test_path(getattr(item, "fspath", "")):
        return False
    nodeid = str(getattr(item, "nodeid", ""))
    return any(matches_critical_test_node(test_key, nodeid) for test_key in CRITICAL_TEST_KEYS)
