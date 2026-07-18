"""Pytest hook plugin emitting exact case outcomes for formal verification."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

_RESULT_PATH_ENV = "BRIDLE_PYTEST_RESULT_PATH"
_PROJECT_ROOT_ENV = "BRIDLE_PYTEST_PROJECT_ROOT"
_case_results: dict[str, dict[str, Any]] = {}
_collection_errors: list[dict[str, Any]] = []


def pytest_sessionstart(session) -> None:
    del session
    _case_results.clear()
    _collection_errors.clear()


def pytest_collectreport(report) -> None:
    if not report.failed:
        return
    _collection_errors.append(
        {
            "node_id": _normalize_node_id(
                str(report.nodeid or ""),
                file_path=getattr(report, "fspath", None),
            ),
            "message": _failure_message(report.longrepr),
        }
    )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    node_id = _normalize_node_id(str(item.nodeid), file_path=item.path)
    if (
        report.when == "call"
        or report.failed
        or (report.skipped and node_id not in _case_results)
    ):
        _case_results[node_id] = _case_result(report, call, node_id=node_id)


def pytest_sessionfinish(session, exitstatus) -> None:
    del session
    raw_path = os.environ.get(_RESULT_PATH_ENV, "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "bridle.pytest_case_results/v1",
        "exit_code": int(exitstatus),
        "case_results": [
            _case_results[node_id]
            for node_id in sorted(_case_results)
        ],
        "collection_errors": list(_collection_errors),
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _case_result(report, call, *, node_id: str) -> dict[str, Any]:
    failure_type = None
    if call.excinfo is not None:
        failure_type = call.excinfo.typename
    return {
        "node_id": node_id,
        "outcome": str(report.outcome),
        "phase": str(report.when),
        "failure_type": failure_type,
    }


def _normalize_node_id(node_id: str, *, file_path=None) -> str:
    suffix = ""
    if "::" in node_id:
        _, suffix = node_id.split("::", 1)
        suffix = f"::{suffix}"
    root = os.environ.get(_PROJECT_ROOT_ENV, "").strip()
    if root and file_path is not None:
        try:
            relative = Path(str(file_path)).resolve().relative_to(Path(root).resolve())
            return f"{relative.as_posix()}{suffix}"
        except ValueError:
            pass
    return node_id.replace("\\", "/")


def _failure_message(longrepr) -> str:
    reprcrash = getattr(longrepr, "reprcrash", None)
    if reprcrash is not None and getattr(reprcrash, "message", None):
        return str(reprcrash.message)[:500]
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[2])[:500]
    lines = [line.strip() for line in str(longrepr).splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:500]
