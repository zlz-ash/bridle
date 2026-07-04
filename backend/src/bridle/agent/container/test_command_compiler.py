"""Compile approved test commands for container entrypoint manifests."""
from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from typing import Any

from bridle.agent.tools.test_command_policy import TestCommandPolicy


@dataclass(frozen=True)
class ApprovedTestCommand:
    command_id: str
    argv: tuple[str, ...]
    raw_command: str
    test_entity_id: str
    map_seq: int


class TestCommandCompiler:
    """Turn map test entities into stable command IDs and argv lists."""

    @staticmethod
    def compile_commands(
        *,
        test_commands: list[str],
        test_entity_id: str,
        map_seq: int,
    ) -> list[ApprovedTestCommand]:
        approved: list[ApprovedTestCommand] = []
        for raw in test_commands:
            text = str(raw).strip()
            if not text:
                continue
            errors = TestCommandPolicy.validate(text)
            if errors:
                raise ValueError(f"test_target_not_allowed: {errors[0]}")
            argv = tuple(shlex.split(text, posix=False))
            command_id = hashlib.sha256(f"{test_entity_id}:{map_seq}:{text}".encode()).hexdigest()[:16]
            approved.append(
                ApprovedTestCommand(
                    command_id=command_id,
                    argv=argv,
                    raw_command=text,
                    test_entity_id=test_entity_id,
                    map_seq=map_seq,
                )
            )
        return approved

    @staticmethod
    def manifest_commands(commands: list[ApprovedTestCommand]) -> list[dict[str, Any]]:
        return [
            {
                "command_id": cmd.command_id,
                "argv": list(cmd.argv),
                "raw_command": cmd.raw_command,
                "test_entity_id": cmd.test_entity_id,
                "map_seq": cmd.map_seq,
            }
            for cmd in commands
        ]
