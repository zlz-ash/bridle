from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import bridle.cli as cli


class _SharedControlService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, operation: str, payload: dict) -> dict:
        self.calls.append((operation, payload))
        return {
            "status": "completed",
            "operation": operation,
            "candidate_id": payload.get("candidate_id"),
            "changed_ids": ["node-1"],
            "artifact_ref": ".bridle/artifacts/candidate.json",
            "error_code": None,
        }


def test_cli_api_and_tool_service_parity(
    test_workspace: Path,
    monkeypatch,
) -> None:
    service = _SharedControlService()
    monkeypatch.setattr(cli, "get_control_service", lambda workspace: service)
    payload = {"candidate_id": "candidate-parity", "cursor": None, "limit": 20}
    expected = service.invoke("candidate", payload)
    service.calls.clear()

    result = CliRunner().invoke(
        cli.app,
        ["candidate", "--workspace", str(test_workspace), "--json"],
        input=json.dumps(payload),
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == expected
    assert service.calls == [("candidate", payload)]

    denied = _SharedControlService()
    denied.invoke = lambda operation, payload: {
        "status": "failed",
        "error_code": "runtime_identity_required",
        "changed_ids": [],
        "artifact_ref": None,
    }
    monkeypatch.setattr(cli, "get_control_service", lambda workspace: denied)
    rejected = CliRunner().invoke(
        cli.app,
        ["verify", "--workspace", str(test_workspace), "--json"],
        input="{}",
    )
    assert rejected.exit_code == 2
    assert json.loads(rejected.output)["error_code"] == "runtime_identity_required"
