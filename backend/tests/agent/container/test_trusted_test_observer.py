"""Tests for the trusted test observer plugin and controller-side event verification."""
from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "trusted_test_observer.py"
SPEC = importlib.util.spec_from_file_location("bridle_trusted_test_observer", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
sys.modules["bridle_trusted_test_observer"] = observer
SPEC.loader.exec_module(observer)

CTRL_SCRIPT = REPO_ROOT / "scripts" / "ci" / "trusted_evidence_controller.py"
CTRL_SPEC = importlib.util.spec_from_file_location("bridle_trusted_test_observer_ctrl", CTRL_SCRIPT)
assert CTRL_SPEC is not None and CTRL_SPEC.loader is not None
controller = importlib.util.module_from_spec(CTRL_SPEC)
sys.modules["bridle_trusted_test_observer_ctrl"] = controller
CTRL_SPEC.loader.exec_module(controller)

LINK_NODE = (
    "backend/tests/agent/container/test_docker_integration.py"
    "::TestDockerCandidateIntegration::test_real_docker_recovers_after_link_attack_in_slot"
)
CHMOD_NODE = (
    "backend/tests/agent/container/test_docker_integration.py"
    "::TestDockerCandidateIntegration"
    "::test_real_docker_recovers_after_rw_root_permission_poisoning"
)


@dataclass
class _ObserverCtx:
    candidate_root: Path
    controller_ipc_dir: Path
    critical_test_nonces: dict[str, str] = field(default_factory=dict)
    consumed_test_event_keys: set[str] = field(default_factory=set)
    sentinel_by_handle: dict[str, Any] = field(default_factory=dict)
    handled_request_ids: set[str] = field(default_factory=set)


def _write_event(directory: Path, event_type: str, test_key: str, nonce: str, **extra: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": observer.EVENT_SCHEMA,
        "event_type": event_type,
        "test_key": test_key,
        "nonce": nonce,
        "worker_pid": 12345,
        **extra,
    }
    path = directory / f"{event_type}_{test_key}.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


class _FakeItem:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


class _FakeReport:
    def __init__(self, when: str, outcome: str) -> None:
        self.when = when
        self.outcome = outcome
        self.longrepr = ""


class _FakeCall:
    def __init__(self, when: str) -> None:
        self.when = when


class _FakeOutcome:
    def __init__(self, report: _FakeReport) -> None:
        self._report = report

    def get_result(self) -> _FakeReport:
        return self._report


def _run_makereport_hook(item: _FakeItem, call: _FakeCall, report: _FakeReport) -> None:
    gen = observer.pytest_runtest_makereport(item, call)
    next(gen)
    with suppress(StopIteration):
        gen.send(_FakeOutcome(report))


def test_observer_match_test_key() -> None:
    assert observer._match_test_key(LINK_NODE) == "link_attack"
    assert observer._match_test_key(CHMOD_NODE) == "chmod_poison"
    assert observer._match_test_key("unrelated::test_foo") is None


def test_observer_collection_records_collected_and_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = tmp_path / "events"
    nonces = {"link_attack": "n1", "chmod_poison": "n2"}
    monkeypatch.setenv("BRIDLE_TEST_EVENTS_DIR", str(events))
    monkeypatch.setenv("BRIDLE_CRITICAL_TEST_NONCES", json.dumps(nonces))
    items = [_FakeItem(LINK_NODE), _FakeItem("unrelated::test_foo")]
    observer.pytest_collection_modifyitems(items)
    link_event = json.loads((events / "collection_link_attack.json").read_text(encoding="utf-8"))
    assert link_event["collected"] is True
    assert link_event["nonce"] == "n1"
    assert link_event["test_node_id"] == LINK_NODE
    chmod_event = json.loads((events / "collection_chmod_poison.json").read_text(encoding="utf-8"))
    assert chmod_event["collected"] is False
    assert chmod_event["test_node_id"] == ""


def test_observer_started_and_finished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = tmp_path / "events"
    monkeypatch.setenv("BRIDLE_TEST_EVENTS_DIR", str(events))
    monkeypatch.setenv("BRIDLE_CRITICAL_TEST_NONCES", json.dumps({"link_attack": "n1"}))
    item = _FakeItem(LINK_NODE)
    observer.pytest_runtest_setup(item)
    _run_makereport_hook(item, _FakeCall("call"), _FakeReport("call", "passed"))
    started = json.loads((events / "started_link_attack.json").read_text(encoding="utf-8"))
    assert started["test_node_id"] == LINK_NODE
    finished = json.loads((events / "finished_link_attack.json").read_text(encoding="utf-8"))
    assert finished["outcome"] == "passed"


def test_controller_verify_rejects_missing_collection(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    result = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id="x")
    assert not result["verified"]
    assert result["reason"] == "collection_event_missing"


def test_controller_verify_rejects_nonce_mismatch(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    events = ipc / "test-events"
    _write_event(events, "collection", "link_attack", "wrong", collected=True, test_node_id="n")
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    result = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id="n")
    assert not result["verified"]
    assert result["reason"] == "collection_nonce_mismatch"


def test_controller_verify_rejects_not_collected(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    events = ipc / "test-events"
    _write_event(events, "collection", "link_attack", "n1", collected=False, test_node_id="")
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    result = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id="")
    assert not result["verified"]
    assert result["reason"] == "test_not_collected"


def test_controller_verify_rejects_missing_finished(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    events = ipc / "test-events"
    _write_event(events, "collection", "link_attack", "n1", collected=True, test_node_id=LINK_NODE)
    _write_event(events, "started", "link_attack", "n1", test_node_id=LINK_NODE)
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    result = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id=LINK_NODE)
    assert not result["verified"]
    assert result["reason"] == "finished_event_missing"


def test_controller_verify_rejects_failed_outcome(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    events = ipc / "test-events"
    _write_event(events, "collection", "link_attack", "n1", collected=True, test_node_id=LINK_NODE)
    _write_event(events, "started", "link_attack", "n1", test_node_id=LINK_NODE)
    _write_event(events, "finished", "link_attack", "n1", outcome="failed", test_node_id=LINK_NODE)
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    result = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id=LINK_NODE)
    assert not result["verified"]
    assert "test_outcome_not_passed" in result["reason"]


def test_controller_verify_rejects_node_id_mismatch(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    events = ipc / "test-events"
    _write_event(events, "collection", "link_attack", "n1", collected=True, test_node_id=LINK_NODE)
    _write_event(events, "started", "link_attack", "n1", test_node_id=LINK_NODE)
    _write_event(events, "finished", "link_attack", "n1", outcome="passed", test_node_id=LINK_NODE)
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    result = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id="forged::node")
    assert not result["verified"]
    assert result["reason"] == "node_id_mismatch"


def test_controller_verify_accepts_full_chain(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    events = ipc / "test-events"
    _write_event(events, "collection", "link_attack", "n1", collected=True, test_node_id=LINK_NODE)
    _write_event(events, "started", "link_attack", "n1", test_node_id=LINK_NODE)
    _write_event(events, "finished", "link_attack", "n1", outcome="passed", test_node_id=LINK_NODE)
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    result = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id=LINK_NODE)
    assert result["verified"]
    assert result["test_node_id"] == LINK_NODE


def test_controller_verify_rejects_replay(tmp_path: Path) -> None:
    ipc = tmp_path / "ipc"
    events = ipc / "test-events"
    _write_event(events, "collection", "link_attack", "n1", collected=True, test_node_id=LINK_NODE)
    _write_event(events, "started", "link_attack", "n1", test_node_id=LINK_NODE)
    _write_event(events, "finished", "link_attack", "n1", outcome="passed", test_node_id=LINK_NODE)
    ctx = _ObserverCtx(candidate_root=tmp_path, controller_ipc_dir=ipc, critical_test_nonces={"link_attack": "n1"})
    first = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id=LINK_NODE)
    assert first["verified"]
    second = controller._verify_test_event_chain(ctx, "link_attack", claimed_node_id=LINK_NODE)
    assert not second["verified"]
    assert second["reason"] == "test_event_already_consumed"
