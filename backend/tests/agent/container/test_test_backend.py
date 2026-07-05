"""Tests for module container test backend verification semantics."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridle.agent.container.candidate_contract import CandidateExecutionRequest
from bridle.agent.container.test_backend import ModuleContainerTestBackend
from bridle.agent.container.test_command_compiler import TestCommandCompiler
from bridle.agent.safety.sandbox_policy import SandboxPolicy


def _backend(required: list[str], *, entity_id: str = "node-1", map_seq: int = 3) -> ModuleContainerTestBackend:
    approved = TestCommandCompiler.compile_commands(
        test_commands=required,
        test_entity_id=entity_id,
        map_seq=map_seq,
    )
    request = CandidateExecutionRequest(
        candidate_id="cand-1",
        run_id="run-1",
        node_id=entity_id,
        project_root=Path("."),
        base_map_seq=map_seq,
        write_set=(),
        read_set=(),
        readonly_files=(),
        tests=tuple(required),
        timeout_seconds=60,
        network_allowed=False,
        module_id="mod-a",
    )
    return ModuleContainerTestBackend(
        MagicMock(),
        candidate_request=request,
        candidate_root="/tmp/cand",
        module_root="/tmp/mod",
        candidate_rel="candidates/cand-1",
        test_entity_id=entity_id,
        required_commands=required,
        required_command_ids=[cmd.command_id for cmd in approved],
        map_seq=map_seq,
    )


@pytest.mark.asyncio
async def test_partial_required_commands_stays_blocked() -> None:
    backend = _backend(["python -m pytest tests/test_a.py -q", "python -m pytest tests/test_b.py -q"])
    backend._backend.run_tests_in_candidate.return_value = {
        "manifest": {"status": "completed"},
        "test_results": [
            {
                "command_id": backend.required_command_ids[0],
                "exit_code": 0,
                "argv": ["python"],
                "raw_command": backend.required_commands[0],
            }
        ],
        "container_id": "fake-1",
        "container_reused": True,
    }
    policy = SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=Path("."),
        allowed_files=[],
        node_tests=backend.required_commands[:1],
    )
    result = await backend.run_allowed_tests(backend.required_commands[:1], policy=policy)
    assert result["status"] == "completed"
    assert backend.evidence.all_required_passed is False


@pytest.mark.asyncio
async def test_all_required_commands_must_pass_for_ready() -> None:
    backend = _backend(["python -m pytest tests/test_a.py -q", "python -m pytest tests/test_b.py -q"])
    ids = backend.required_command_ids

    async def _run_once(commands: list[str]) -> dict:
        backend._backend.run_tests_in_candidate.return_value = {
            "manifest": {"status": "completed"},
            "test_results": [
                {
                    "command_id": ids[0 if "test_a" in commands[0] else 1],
                    "exit_code": 0,
                    "argv": ["python"],
                    "raw_command": commands[0],
                }
            ],
            "container_id": "fake-1",
            "container_reused": True,
        }
        policy = SandboxPolicy.for_run(
            run_id="run-1",
            node_id="node-1",
            workspace_root=Path("."),
            allowed_files=[],
            node_tests=commands,
        )
        return await backend.run_allowed_tests(commands, policy=policy)

    await _run_once([backend.required_commands[0]])
    assert backend.evidence.all_required_passed is False
    await _run_once([backend.required_commands[1]])
    assert backend.evidence.all_required_passed is True
