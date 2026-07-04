#!/usr/bin/env python3
"""Malicious candidate-side isolation probe fixtures (not trusted code)."""
from __future__ import annotations

from pathlib import Path

PROBE_DIR = ".bridle-isolation-probe"


def probe_root(candidate_root: Path) -> Path:
    return candidate_root / PROBE_DIR


def write_probe_files(candidate_root: Path) -> Path:
    root = probe_root(candidate_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "test_probe.py").write_text(
        "def test_probe():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    (root / "conftest.py").write_text(_MALICIOUS_CONFTEST, encoding="utf-8")
    return root


_MALICIOUS_CONFTEST = '''
"""Candidate-controlled malicious probe executed inside worker only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROBE_REPORT_PREFIX = "BRIDLE_ISOLATION_PROBE_REPORT:"


def _candidate_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _attempt_sys_modules_tamper() -> dict:
    import pytest as pytest_module

    original = sys.modules.get("pytest")
    try:
        sys.modules["pytest"] = object()  # type: ignore[assignment]
        return {"attempted": True, "succeeded": sys.modules.get("pytest") is not pytest_module}
    except OSError as exc:
        return {"attempted": True, "succeeded": False, "error": str(exc)}
    finally:
        if original is None:
            sys.modules.pop("pytest", None)
        else:
            sys.modules["pytest"] = original


def _attempt_pytest_monkeypatch() -> dict:
    try:
        import pytest

        original = pytest.main
        pytest.main = lambda *args, **kwargs: 0  # type: ignore[method-assign, assignment]
        succeeded = pytest.main is not original
        pytest.main = original
        return {"attempted": True, "succeeded": succeeded}
    except OSError as exc:
        return {"attempted": True, "succeeded": False, "error": str(exc)}


def _attempt_evidence_write() -> dict:
    candidate = _candidate_root()
    targets = [
        Path("/trusted-config/malicious-evidence.json"),
        Path("/trusted-scripts/malicious-evidence.json"),
        candidate / ".." / "evidence" / "malicious-evidence.json",
    ]
    outcomes = []
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"forged": true}\\n', encoding="utf-8")
            outcomes.append({"path": str(target), "succeeded": target.exists()})
        except OSError as exc:
            outcomes.append({"path": str(target), "succeeded": False, "error": str(exc)})
    return {"attempted": True, "outcomes": outcomes}


def _attempt_control_env_read() -> dict:
    blocked = (
        "BRIDLE_DOCKER_EVIDENCE_DIR",
        "BRIDLE_TRUSTED_HARNESS_ROOT",
        "BRIDLE_CONTROLLER_SECRET",
    )
    leaked = {name: os.environ.get(name) for name in blocked}
    return {
        "attempted": True,
        "succeeded": any(value is not None for value in leaked.values()),
        "observed": {name: value is not None for name, value in leaked.items()},
    }


def _attempt_harness_override() -> dict:
    targets = [
        Path("/trusted-config/pyproject.toml"),
        Path("/trusted-scripts/trusted_harness.py"),
        Path("/trusted-scripts/candidate_worker.py"),
    ]
    outcomes = []
    for target in targets:
        try:
            with target.open("a", encoding="utf-8") as handle:
                handle.write("\\n# candidate override attempt\\n")
            outcomes.append({"path": str(target), "succeeded": True})
        except OSError as exc:
            outcomes.append({"path": str(target), "succeeded": False, "error": str(exc)})
    return {"attempted": True, "outcomes": outcomes}


def pytest_configure(config) -> None:
    if os.environ.get("BRIDLE_ISOLATION_PROBE") != "1":
        return
    report = {
        "sys_modules_tamper": _attempt_sys_modules_tamper(),
        "pytest_monkeypatch": _attempt_pytest_monkeypatch(),
        "evidence_write": _attempt_evidence_write(),
        "control_env_read": _attempt_control_env_read(),
        "harness_override": _attempt_harness_override(),
    }
    print(PROBE_REPORT_PREFIX + json.dumps(report, sort_keys=True), flush=True)
'''.lstrip()
