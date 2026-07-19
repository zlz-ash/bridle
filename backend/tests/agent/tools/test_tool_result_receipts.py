from __future__ import annotations

import json

from bridle.agent.memory import short_term_memory as memory_module


def test_tool_result_is_full_for_one_consumption_then_replaced_by_receipt() -> None:
    builder_type = memory_module.ToolResultReceiptBuilder
    raw = json.dumps(
        {
            "status": "completed",
            "success": True,
            "id": "artifact-7",
            "path": "src/example.py",
            "sha256": "abc123",
            "cursor": "next-9",
            "payload": "large unknown value " * 200,
        }
    )

    receipt = json.loads(builder_type.build("run_command", raw))

    assert receipt == {
        "cursor": "next-9",
        "id": "artifact-7",
        "path": "src/example.py",
        "sha256": "abc123",
        "status": "completed",
        "success": True,
        "tool_name": "run_command",
    }
    assert "payload" not in receipt
    assert builder_type.build("run_command", raw) == builder_type.build(
        "run_command",
        json.dumps(json.loads(raw), sort_keys=True),
    )


def test_failed_tool_result_receipt_is_deterministic_and_diagnostic() -> None:
    builder_type = memory_module.ToolResultReceiptBuilder
    first = {
        "status": "failed",
        "success": False,
        "error_code": "command_failed",
        "error_type": "process",
        "exit_code": 17,
        "message": "compiler rejected input",
        "unknown": "must be dropped",
    }
    second = dict(reversed(list(first.items())))

    first_receipt = builder_type.build("run_command", json.dumps(first))
    second_receipt = builder_type.build("run_command", json.dumps(second))

    assert first_receipt == second_receipt
    assert json.loads(first_receipt) == {
        "error_code": "command_failed",
        "error_summary": "compiler rejected input",
        "error_type": "process",
        "exit_code": 17,
        "status": "failed",
        "success": False,
        "tool_name": "run_command",
    }

    long_message = "compiler rejected input " + "x" * 2_000
    first["message"] = long_message
    bounded_receipt = builder_type.build("run_command", json.dumps(first))
    bounded_payload = json.loads(bounded_receipt)

    assert bounded_payload["error_summary"] == long_message[:240]
    assert "unknown" not in bounded_payload
    assert len(bounded_receipt.encode("utf-8")) <= 512
