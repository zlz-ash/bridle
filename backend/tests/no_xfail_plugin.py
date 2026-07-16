from __future__ import annotations

import pytest

_saw_wasxfail = False


def pytest_sessionstart() -> None:
    global _saw_wasxfail
    _saw_wasxfail = False


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    violations = [item.nodeid for item in items if item.get_closest_marker("xfail") is not None]
    if violations:
        joined = ", ".join(violations)
        raise pytest.UsageError(f"xfail markers are forbidden: {joined}")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _saw_wasxfail
    if getattr(report, "wasxfail", None) is not None:
        _saw_wasxfail = True


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _saw_wasxfail:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
